from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import pytest


MODULE_PATH = Path(__file__).with_name("connector.py")
SPEC = importlib.util.spec_from_file_location("ado_connector", MODULE_PATH)
assert SPEC and SPEC.loader
connector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = connector
SPEC.loader.exec_module(connector)


PAT = "fixture-pat-do-not-log"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class FakeBoard:
    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}
        self.create_calls: list[str] = []

    def create_ticket(self, ticket_id: str, body: Mapping[str, Any]) -> str:
        self.create_calls.append(ticket_id)
        self.created.setdefault(ticket_id, dict(body))
        self.tickets.setdefault(
            ticket_id,
            {
                "ticket_id": ticket_id,
                "status": "open",
                "summary": "",
                "submission_history": [],
            },
        )
        return ticket_id

    def get_ticket(self, ticket_id: str) -> Mapping[str, Any]:
        return self.tickets[ticket_id]

    def close(self, ticket_id: str, commit: str = COMMIT_A) -> None:
        self.tickets[ticket_id].update(
            {
                "status": "closed",
                "summary": "Scanner remediation completed",
                "submission_history": [
                    {"notes": f"commit_hash: {commit}", "summary": "Done"}
                ],
            }
        )


def make_config(tmp_path: Path, base_url: str) -> Any:
    token = tmp_path / "central.token"
    token.write_text("opaque-central-token", encoding="utf-8")
    return connector.ConnectorConfig(
        ado=connector.AdoSettings(base_url, "demo", "repo", "ADO_TEST_PAT"),
        central=connector.CentralSettings("https://central.invalid/mcp", token),
        board=connector.BoardSettings("scratch", "pursers/ado"),
        filters=connector.FilterSettings(
            authors=("scanner-bot",),
            labels=("finding",),
            vote_reviewer_id="connector-seat",
            closed_vote=0,
        ),
        poll_seconds=1,
        state_file=tmp_path / "state.json",
    )


def make_runtime(tmp_path: Path, fixture: Any, server: Any) -> tuple[Any, FakeBoard]:
    config = make_config(tmp_path, server.base_url)
    board = FakeBoard()
    runtime = connector.Connector(
        config,
        connector.AdoClient(config.ado, PAT),
        board,
        connector.StateStore(config.state_file),
    )
    return runtime, board


def test_sweep_deduplicates_and_updated_commit_creates_linked_ticket(
    tmp_path: Path,
) -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(17, "Fix generated finding", COMMIT_A)
    fixture.add_thread(17, "Finding: unsafe example")
    with connector.FakeAdoServer(fixture) as server:
        runtime, board = make_runtime(tmp_path, fixture, server)
        first = runtime.cycle()
        second = runtime.cycle()
        fixture.update_commit(17, COMMIT_B)
        third = runtime.cycle()

    first_id = connector.deterministic_ticket_id(17, COMMIT_A)
    second_id = connector.deterministic_ticket_id(17, COMMIT_B)
    assert first == [f"created {first_id} for PR 17@{COMMIT_A[:12]}"]
    assert second == []
    assert third == [f"created {second_id} for PR 17@{COMMIT_B[:12]}"]
    assert board.create_calls == [first_id, second_id]
    assert first_id in board.created[second_id]["description"]
    assert "unsafe example" in board.created[first_id]["description"]
    assert board.created[first_id]["tags"] == ["connector-ado", "ado-pr-17"]


def test_writeback_comment_and_vote_are_idempotent_across_retry(tmp_path: Path) -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(4, "Fix finding", COMMIT_A)
    with connector.FakeAdoServer(fixture) as server:
        runtime, board = make_runtime(tmp_path, fixture, server)
        runtime.cycle()
        ticket_id = connector.deterministic_ticket_id(4, COMMIT_A)
        board.close(ticket_id)
        actions = runtime.cycle()
        assert len(fixture.comment_posts) == 1
        assert fixture.votes[(4, "connector-seat")] == 0
        assert "not a merge or human approval" in fixture.comment_posts[0][1]
        assert COMMIT_A in fixture.comment_posts[0][1]

        # Simulate a crash after the remote comment/vote but before local flags.
        state = runtime.state_store.load()
        item = state["items"][connector.state_key(4, COMMIT_A)]
        item["commented"] = False
        item["voted"] = False
        runtime.state_store.save(state)
        retried = runtime.cycle()

    assert actions == [
        f"commented PR 4 for {ticket_id}",
        f"set connector vote 0 on PR 4 for {ticket_id}",
    ]
    assert len(fixture.comment_posts) == 1
    assert retried == [
        f"commented PR 4 for {ticket_id}",
        f"set connector vote 0 on PR 4 for {ticket_id}",
    ]


def test_filter_requires_configured_author_and_label(tmp_path: Path) -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(1, "Wrong author", COMMIT_A, author="human", labels=("finding",))
    fixture.add_pr(2, "Wrong label", COMMIT_A, author="scanner-bot", labels=("other",))
    fixture.add_pr(3, "Matches", COMMIT_A, author="scanner-bot", labels=("finding",))
    with connector.FakeAdoServer(fixture) as server:
        runtime, board = make_runtime(tmp_path, fixture, server)
        runtime.cycle()
    assert board.create_calls == [connector.deterministic_ticket_id(3, COMMIT_A)]


def test_pat_authentication_and_secret_never_enter_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(1, "Finding", COMMIT_A)
    with connector.FakeAdoServer(fixture) as server:
        settings = connector.AdoSettings(server.base_url, "demo", "repo", "ADO_TEST_PAT")
        caplog.set_level(logging.DEBUG)
        with pytest.raises(connector.AdoError, match="HTTP 401"):
            connector.AdoClient(settings, "wrong-secret").list_pull_requests()
        rows = connector.AdoClient(settings, PAT).list_pull_requests()
    rendered = caplog.text
    assert len(rows) == 1
    assert fixture.auth_failures == 1
    assert PAT not in rendered
    assert "wrong-secret" not in rendered


def test_cli_scrubs_unexpected_exception_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        connector.ConnectorConfig,
        "load",
        classmethod(lambda _cls, _path: object()),
    )
    monkeypatch.setattr(
        connector,
        "run_connector",
        lambda _config, _once: (_ for _ in ()).throw(RuntimeError(PAT)),
    )
    caplog.set_level(logging.ERROR)
    with pytest.raises(SystemExit) as raised:
        connector.main(["run", "--config", "unused.json", "--once"])
    assert raised.value.code == 3
    assert "RuntimeError" in caplog.text
    assert PAT not in caplog.text


def test_config_requires_0600_and_contains_only_pat_environment_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "central.token"
    token.write_text("opaque", encoding="utf-8")
    config_path = tmp_path / "config.json"
    raw = {
        "ado": {
            "base_url": "https://ado.example.invalid/org",
            "project": "demo",
            "repo": "repo",
            "pat_env": "ADO_CONNECTOR_PAT",
        },
        "central": {
            "url": "https://127.0.0.1:8766/mcp",
            "token_path": str(token),
        },
        "board": {
            "id": "pursers",
            "target_url_prefix": "pursers/ado",
        },
        "filters": {
            "authors": ["scanner-bot"],
            "labels": ["finding"],
            "closed_vote": 0,
        },
        "poll_seconds": 30,
        "state_file": "state.json",
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(config_path, 0o644)
    with pytest.raises(connector.ConfigError, match="0600"):
        connector.ConnectorConfig.load(config_path)
    os.chmod(config_path, 0o600)
    loaded = connector.ConnectorConfig.load(config_path)
    assert loaded.ado.pat_env == "ADO_CONNECTOR_PAT"
    assert loaded.central.create_mode == "intake"
    assert loaded.state_file == tmp_path / "state.json"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert PAT not in config_path.read_text()

    raw["filters"]["closed_vote"] = 10
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(connector.ConfigError, match="approval votes are forbidden"):
        connector.ConnectorConfig.load(config_path)


def test_default_gateway_uses_narrow_intake_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pursers_client

    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            captured.update({"name": name, "arguments": arguments})
            return {"ticket": {"ticket_id": arguments["ticket_id"]}}

        async def ticket_create(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("intake mode must use the narrow call shape")

    monkeypatch.setattr(pursers_client, "BoardClient", FakeClient)
    config = make_config(tmp_path, "https://ado.invalid")
    gateway = connector.PursersBoardGateway(config)
    ticket_id = "TK-ado-7-aaaaaaaaaaaa"
    body = {
        "title": "Finding",
        "description": "Description",
        "tags": ["connector-ado", "ado-pr-7"],
        "target_url": "pursers/ado/7/aaaaaaaaaaaa",
    }
    assert gateway.create_ticket(ticket_id, body) == ticket_id
    assert captured["name"] == "ticket_create"
    assert captured["arguments"]["coordinator_op_key"] == connector.create_op_key(ticket_id)
    assert captured["arguments"]["unassigned"] is True


def test_state_corruption_is_moved_aside_and_recovers_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    store = connector.StateStore(path)
    state = store.load()
    assert state == connector.StateStore.empty()
    assert not path.exists()
    recovered = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(recovered) == 1
    assert recovered[0].read_text() == "{broken"
    store.save(state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_structurally_corrupt_state_item_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": {"bad": {"pr_id": "not-an-integer"}},
            }
        ),
        encoding="utf-8",
    )
    assert connector.StateStore(path).load() == connector.StateStore.empty()
    assert list(tmp_path.glob("state.json.corrupt-*"))


def test_thread_summary_is_bounded() -> None:
    threads = [
        {"comments": [{"content": "x" * 500}]}
        for _index in range(20)
    ]
    rendered = connector.thread_summary(threads)
    assert len(rendered) <= connector.MAX_THREAD_SUMMARY_CHARS
    assert rendered.startswith("20 thread(s)")


def test_fake_server_rejects_approval_votes() -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(8, "Finding", COMMIT_A)
    with connector.FakeAdoServer(fixture) as server:
        settings = connector.AdoSettings(server.base_url, "demo", "repo", "ADO_TEST_PAT")
        client = connector.AdoClient(settings, PAT)
        with pytest.raises(connector.AdoError, match="HTTP 400"):
            client.set_vote(8, "connector-seat", 10)
    assert fixture.votes == {}


def test_end_to_end_fake_ado_scratch_board_flow(tmp_path: Path) -> None:
    fixture = connector.FakeAdoFixture(PAT)
    fixture.add_pr(99, "Repair scanner finding", COMMIT_A)
    fixture.add_thread(99, "Reproduce by running the fixture")
    with connector.FakeAdoServer(fixture) as server:
        runtime, board = make_runtime(tmp_path, fixture, server)
        created = runtime.cycle()
        ticket_id = connector.deterministic_ticket_id(99, COMMIT_A)
        board.close(ticket_id, COMMIT_B)
        written = runtime.cycle()

    assert created == [f"created {ticket_id} for PR 99@{COMMIT_A[:12]}"]
    assert written == [
        f"commented PR 99 for {ticket_id}",
        f"set connector vote 0 on PR 99 for {ticket_id}",
    ]
    assert len(fixture.comment_posts) == 1
    assert COMMIT_B in fixture.comment_posts[0][1]
    assert fixture.votes == {(99, "connector-seat"): 0}


def test_end_to_end_real_central_scratch_board_with_intake_scope(
    tmp_path: Path,
) -> None:
    central_source = MODULE_PATH.parents[2] / "packages" / "central" / "src" / "pursers_central"
    sys.path.insert(0, str(central_source))
    import central as central_server

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    environment = patch.dict(
        os.environ,
        {
            "CENTRAL_AUTH_MODE": "jwt",
            "CENTRAL_JWT_ISSUER": "https://issuer.example",
            "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
            "CENTRAL_JWKS_PATH": str(jwks),
            "CENTRAL_ADMISSION": "invite",
            "STORE_BACKEND": "sqlite",
        },
    )
    environment.start()
    original_current_principal = central_server.current_principal
    try:
        mcp, _service = central_server.build_server(
            "localhost", 8765, tmp_path / "central-data"
        )
        admin = central_server.Principal(
            "PR-admin", "admin", frozenset({"board:read", "board:write", "board:review"})
        )
        intake = central_server.Principal(
            "PR-intake",
            "intake",
            frozenset({"board:read", "board:coordinate", "board:intake"}),
        )
        worker = central_server.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        reviewer = central_server.Principal(
            "PR-reviewer",
            "reviewer",
            frozenset({"board:read", "board:write", "board:review"}),
        )

        async def call(principal: Any, name: str, **arguments: Any) -> Any:
            central_server.current_principal = lambda: principal
            result = await mcp.call_tool(name, {"board_id": "scratch", **arguments})
            assert not result.is_error, result.content
            return result.structured_content

        asyncio.run(call(admin, "board_join", agent_name="admin"))
        for principal in (intake, worker, reviewer):
            asyncio.run(
                call(
                    admin,
                    "board_member_add",
                    agent_name="admin",
                    principal_id=principal.principal_id,
                    role="reviewer" if principal is reviewer else "member",
                )
            )
        asyncio.run(call(intake, "board_join", agent_name="ado-connector"))
        asyncio.run(call(worker, "board_join", agent_name="worker"))
        asyncio.run(call(reviewer, "board_join", agent_name="reviewer"))

        class CentralScratchBoard:
            def create_ticket(self, ticket_id: str, body: Mapping[str, Any]) -> str:
                result = asyncio.run(
                    call(
                        intake,
                        "ticket_create",
                        agent_name="ado-connector",
                        ticket_id=ticket_id,
                        title=body["title"],
                        description=body["description"],
                        scope="interactive-no-send",
                        required_fields=["commit_hash", "test_output"],
                        tags=body["tags"],
                        target_url=body["target_url"],
                        unassigned=True,
                        coordinator_op_key=connector.create_op_key(ticket_id),
                    )
                )
                return str(result["ticket"]["ticket_id"])

            def get_ticket(self, ticket_id: str) -> Mapping[str, Any]:
                result = asyncio.run(call(intake, "ticket_get", ticket_id=ticket_id))
                return result["ticket"]

        fixture = connector.FakeAdoFixture(PAT)
        fixture.add_pr(123, "Central integration finding", COMMIT_A)
        with connector.FakeAdoServer(fixture) as server:
            config = make_config(tmp_path, server.base_url)
            config = connector.ConnectorConfig(
                config.ado,
                config.central,
                connector.BoardSettings("scratch", "scratch/ado"),
                config.filters,
                config.poll_seconds,
                config.state_file,
            )
            runtime = connector.Connector(
                config,
                connector.AdoClient(config.ado, PAT),
                CentralScratchBoard(),
                connector.StateStore(config.state_file),
            )
            created = runtime.cycle()
            ticket_id = connector.deterministic_ticket_id(123, COMMIT_A)
            created_ticket = asyncio.run(
                call(intake, "ticket_get", ticket_id=ticket_id)
            )["ticket"]
            assert created_ticket["origin"] == "coordinator-intake"
            assert created_ticket["coordinator_op_key"] == connector.create_op_key(ticket_id)

            asyncio.run(call(worker, "ticket_claim", agent_name="worker", ticket_id=ticket_id))
            asyncio.run(
                call(
                    worker,
                    "ticket_submit",
                    agent_name="worker",
                    ticket_id=ticket_id,
                    summary="Resolved scanner finding",
                    notes=f"commit_hash: {COMMIT_B}",
                    files_changed=["src/example.py"],
                    stay_active=False,
                )
            )
            asyncio.run(
                call(
                    reviewer,
                    "ticket_review",
                    agent_name="reviewer",
                    ticket_id=ticket_id,
                    verdict="approve",
                    review_notes="Verified on scratch Central",
                )
            )
            written = runtime.cycle()

        assert created == [f"created {ticket_id} for PR 123@{COMMIT_A[:12]}"]
        assert written == [
            f"commented PR 123 for {ticket_id}",
            f"set connector vote 0 on PR 123 for {ticket_id}",
        ]
        assert COMMIT_B in fixture.comment_posts[0][1]
        assert fixture.votes[(123, "connector-seat")] == 0
        print(
            json.dumps(
                {
                    "fake_ado_pr": "123@" + COMMIT_A[:12],
                    "scratch_board": "scratch",
                    "created_ticket": ticket_id,
                    "ticket_origin": "coordinator-intake",
                    "ticket_status": "closed",
                    "writeback_comment_count": len(fixture.comment_posts),
                    "submitted_commit": COMMIT_B,
                    "connector_vote": fixture.votes[(123, "connector-seat")],
                },
                sort_keys=True,
            )
        )
    finally:
        central_server.current_principal = original_current_principal
        environment.stop()
