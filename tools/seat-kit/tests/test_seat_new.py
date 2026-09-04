from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("seat_new", ROOT / "seat_new.py")
assert SPEC and SPEC.loader
seat_new = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seat_new)


def args(
    tmp_path: Path,
    *,
    role: str = "worker",
    repo: str | None = None,
    client: str = "codex",
):
    return seat_new.build_parser().parse_args(
        [
            "--role",
            role,
            "--name",
            f"{role}-a",
            "--dest",
            str(tmp_path / role),
            "--central-url",
            "https://central.example/mcp",
            "--token-file",
            str(tmp_path / "seat.jwt"),
            "--ca-file",
            str(tmp_path / "ca.pem"),
            "--client",
            client,
            *(["--repo", repo] if repo else []),
        ]
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def load_generated(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalSubscriptionAdapter:
    """Approved BoardClient.events contract over a real in-process Central."""

    def __init__(self, raw_client: Any, service: Any, agent_id: str) -> None:
        self.raw_client = raw_client
        self.service = service
        self.identity = SimpleNamespace(agent_id=agent_id)
        self.ready = asyncio.Event()
        self.events_calls: list[dict[str, Any]] = []
        self.ticket_list_calls = 0
        self.catchup_calls = 0

    async def ticket_list(self, **_arguments: Any) -> dict[str, Any]:
        self.ticket_list_calls += 1
        raise AssertionError("default wait must not call ticket_list")

    async def board_catchup(self, **_arguments: Any) -> dict[str, Any]:
        self.catchup_calls += 1
        raise AssertionError("default wait must delegate pure refetch to events()")

    async def events(
        self,
        from_cursor: int | None = None,
        *,
        only_mine: bool = True,
        kinds: frozenset[str] | None = None,
        resource_subscriptions: tuple[str, ...] | None = None,
        acknowledge: bool = True,
        touch: bool | None = None,
        cursor_callback: Any = None,
    ):
        selected = kinds or frozenset()
        cursor = int(from_cursor or 0)
        subscriptions = tuple(resource_subscriptions or ())
        self.events_calls.append(
            {
                "from_cursor": from_cursor,
                "only_mine": only_mine,
                "kinds": selected,
                "resource_subscriptions": subscriptions,
                "acknowledge": acknowledge,
                "touch": touch,
            }
        )
        assert acknowledge is False
        assert touch is False
        if cursor_callback is not None:
            cursor_callback(cursor)
        async with self.raw_client.listen(
            resource_subscriptions=list(subscriptions)
        ) as subscription:
            self.ready.set()
            async for _cue in subscription:
                page = self.service.journal.read_after("pursers", cursor, 100)
                cursor = int(page["next_cursor"])
                if cursor_callback is not None:
                    cursor_callback(cursor)
                for event in page["events"]:
                    if event.get("kind") not in selected:
                        continue
                    if event.get("actor") == self.identity.agent_id:
                        continue
                    if only_mine and self.identity.agent_id not in event.get(
                        "recipient_identities", []
                    ):
                        continue
                    yield event


def persisted_documents(service: Any) -> list[tuple[str, str, int]]:
    connection = sqlite3.connect(service.store.db_path)
    try:
        return connection.execute(
            "SELECT path, doc, version FROM documents ORDER BY path"
        ).fetchall()
    finally:
        connection.close()


def test_worker_folder_permissions_and_secret_safety(tmp_path: Path) -> None:
    secret = "SECRET_MUST_NOT_BE_COPIED"
    (tmp_path / "seat.jwt").write_text(secret, encoding="utf-8")
    (tmp_path / "ca.pem").write_text("synthetic CA", encoding="utf-8")

    dest = seat_new.generate(args(tmp_path))

    assert mode(dest) == 0o700
    assert mode(dest / "bin") == 0o755
    assert mode(dest / "bin" / "board.sh") == 0o755
    assert mode(dest / "bin" / "board.py") == 0o644
    assert (dest / "AGENTS.md").read_text() == (dest / ".goosehints").read_text()
    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            dest / "bin" / "board.sh",
            dest / "bin" / "board.py",
            dest / "AGENTS.md",
        )
    )
    assert secret not in generated
    assert "worker-a" in generated
    assert "ticket_review" in generated
    assert "never call ticket_review" in generated


def test_worker_and_reviewer_variants_have_only_their_commands(tmp_path: Path) -> None:
    worker = seat_new.generate(args(tmp_path, role="worker"))
    reviewer = seat_new.generate(args(tmp_path, role="reviewer"))

    worker_py = (worker / "bin" / "board.py").read_text(encoding="utf-8")
    reviewer_py = (reviewer / "bin" / "board.py").read_text(encoding="utf-8")
    assert "ROLE = 'worker'" in worker_py
    assert 'commands.add_parser("claim")' in worker_py
    assert "ROLE = 'reviewer'" in reviewer_py
    assert 'commands.add_parser("approve")' in reviewer_py
    assert "reviewers never claim/submit/write code/push" in (
        reviewer / "AGENTS.md"
    ).read_text()


def test_repo_clone_uses_repo_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append(command)
        Path(command[-1]).mkdir()

    monkeypatch.setattr(seat_new.subprocess, "run", fake_run)
    dest = seat_new.generate(args(tmp_path, repo="https://example.test/acme/Pursers.git"))

    assert calls == [
        [
            "git",
            "clone",
            "--",
            "https://example.test/acme/Pursers.git",
            str(dest / "Pursers"),
        ]
    ]
    assert "REPO_LEAF = 'Pursers'" in (dest / "bin" / "board.py").read_text()


def test_board_sh_missing_token_fails_cleanly_without_network(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path))

    result = subprocess.run(
        [str(dest / "bin" / "board.sh"), "list"],
        cwd=dest,
        text=True,
        capture_output=True,
        env={**os.environ, "PURSERS_TOKEN_FILE": str(tmp_path / "missing.jwt")},
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("board.sh: token file is not readable:")
    assert "Traceback" not in result.stderr


def test_nonempty_destination_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "worker"
    dest.mkdir()
    (dest / "keep.txt").write_text("owned by user", encoding="utf-8")

    with pytest.raises(ValueError, match="not empty"):
        seat_new.generate(args(tmp_path))

    assert (dest / "keep.txt").read_text() == "owned by user"


@pytest.mark.parametrize(
    ("client", "host_timeout", "wait_timeout"),
    [
        ("goose", 300, 270),
        ("codex", 620, 560),
        ("claude", 21_600, 21_540),
        ("generic", 180, 150),
    ],
)
def test_client_profile_renders_derived_wait_default(
    tmp_path: Path, client: str, host_timeout: int, wait_timeout: int
) -> None:
    dest = seat_new.generate(args(tmp_path / client, client=client))
    generated = load_generated(dest / "bin" / "board.py", f"board_{client}")
    parsed = generated._parser().parse_args(["wait"])
    instructions = (dest / "AGENTS.md").read_text(encoding="utf-8")

    assert parsed.timeout == wait_timeout
    assert f"{host_timeout}s/{wait_timeout}s" in instructions
    assert "sleep 90-120" not in instructions
    assert "wait --poll" in instructions
    if client == "goose":
        assert "`timeout: 3600`" in instructions
        assert "`board.sh wait --timeout 3540`" in instructions


def test_goose_generator_prints_exact_timeout_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = seat_new.main(
        [
            "--role",
            "worker",
            "--name",
            "goose-worker",
            "--dest",
            str(tmp_path / "goose-worker"),
            "--central-url",
            "https://central.example/mcp",
            "--token-file",
            str(tmp_path / "seat.jwt"),
            "--ca-file",
            str(tmp_path / "ca.pem"),
            "--client",
            "goose",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "config.yaml line: timeout: 3600" in output
    assert "board.sh wait --timeout 3540 --since <cursor>" in output


def test_generated_wait_requires_approved_pure_client_api(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_legacy")

    class LegacyClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        async def events(self, from_cursor=None, *, kinds=None):
            if False:
                yield None

    with pytest.raises(RuntimeError, match="approved pure subscription API"):
        asyncio.run(
            generated._cmd_wait(
                LegacyClient(), "pursers", 4, 1, poll_fallback=False
            )
        )


def test_polling_requires_explicit_flag_and_uses_pure_catchup(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_poll")

    class PollClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
            self.calls.append(arguments)
            return {
                "next_cursor": 8,
                "events": [
                    {
                        "seq": 8,
                        "kind": "ticket_created",
                        "ticket_id": "TK-ready",
                        "recipient_identities": ["AI-worker"],
                    }
                ],
            }

    client = PollClient()
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            generated._cmd_wait(
                client, "pursers", 7, 1, poll_fallback=True
            )
        )
    result = json.loads(output.getvalue())

    assert result["new_seq"] == 8
    assert result["timed_out"] is False
    assert client.calls == [{"cursor": 7, "limit": 50, "ack": False, "touch": False}]


def test_event_wait_closes_stream_before_printing_result(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_close_event")

    class EventClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        def __init__(self) -> None:
            self.closed = False

        async def events(
            self,
            from_cursor: int | None = None,
            *,
            only_mine: bool = True,
            kinds: frozenset[str] | None = None,
            resource_subscriptions: tuple[str, ...] | None = None,
            acknowledge: bool = True,
            touch: bool | None = None,
            cursor_callback: Any = None,
        ):
            try:
                yield {
                    "id": "EV-ready",
                    "seq": 8,
                    "kind": "ticket_created",
                    "ticket_id": "TK-ready",
                }
            finally:
                self.closed = True

    client = EventClient()
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            generated._cmd_wait(
                client, "pursers", 7, 1, poll_fallback=False
            )
        )
    result = json.loads(output.getvalue())

    assert client.closed is True
    assert result["new_seq"] == 8
    assert result["timed_out"] is False
    assert [event["id"] for event in result["events"]] == ["EV-ready"]


async def build_local_central(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pursers_central import central

    tmp_path.mkdir(parents=True, exist_ok=True)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks_path))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, service = central.build_server("localhost", 8765, tmp_path / "data")
    principals = {
        "admin": central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        ),
        "worker": central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
        ),
        "reviewer": central.Principal(
            "PR-reviewer",
            "reviewer-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        ),
    }
    active = {"principal": principals["admin"]}
    original_current_principal = central.current_principal
    central.current_principal = lambda: active["principal"]

    async def call(name: str, **arguments: Any) -> Any:
        return await mcp.call_tool(name, {"board_id": "pursers", **arguments})

    joined = await call("board_join", agent_name="admin-agent")
    agent_ids = {"admin": joined.structured_content["agent_id"]}
    for key in ("worker", "reviewer"):
        await call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=principals[key].principal_id,
            role="member",
        )
        active["principal"] = principals[key]
        joined = await call("board_join", agent_name=f"{key}-agent")
        agent_ids[key] = joined.structured_content["agent_id"]
        active["principal"] = principals["admin"]
    return central, mcp, service, principals, active, agent_ids, call, original_current_principal


def test_goose_generated_wait_60_second_idle_is_pure_and_rearms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client

        dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
        generated = load_generated(dest / "bin" / "board.py", "board_goose_idle")
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            _call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        try:
            active["principal"] = principals["worker"]
            cursor = int(service.journal.read_after("pursers", 0)["latest_cursor"])
            before = persisted_documents(service)
            async with Client(mcp, mode="2026-07-28", cache=None) as raw_client:
                adapter = LocalSubscriptionAdapter(
                    raw_client, service, agent_ids["worker"]
                )
                output = io.StringIO()
                started = time.monotonic()
                with redirect_stdout(output):
                    await generated._cmd_wait(
                        adapter, "pursers", cursor, 60, poll_fallback=False
                    )
                elapsed = time.monotonic() - started
                result = json.loads(output.getvalue())

                assert 59.5 <= elapsed < 65
                assert result["timed_out"] is True
                assert result["events"] == []
                assert result["new_seq"] == cursor
                assert adapter.ticket_list_calls == 0
                assert adapter.catchup_calls == 0
                assert adapter.events_calls == [
                    {
                        "from_cursor": cursor,
                        "only_mine": True,
                        "kinds": frozenset(
                            {"ticket_created", "ticket_status_changed"}
                        ),
                        "resource_subscriptions": (
                            "board://pursers/journal",
                            f"board://pursers/agent/{agent_ids['worker']}",
                        ),
                        "acknowledge": False,
                        "touch": False,
                    }
                ]
                assert persisted_documents(service) == before

                rearm_output = io.StringIO()
                with redirect_stdout(rearm_output):
                    await generated._cmd_wait(
                        adapter,
                        "pursers",
                        result["new_seq"],
                        1,
                        poll_fallback=False,
                    )
                rearmed = json.loads(rearm_output.getvalue())
                assert rearmed["new_seq"] == result["new_seq"]
                assert rearmed["timed_out"] is True
                assert persisted_documents(service) == before
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_reviewer_wait_submitted_wakes_on_real_central_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client

        dest = seat_new.generate(
            args(tmp_path / "seat", role="reviewer", client="goose")
        )
        generated = load_generated(dest / "bin" / "board.py", "board_reviewer")
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        try:
            active["principal"] = principals["admin"]
            created = await call(
                "ticket_create",
                agent_name="admin-agent",
                title="review wait fixture",
                description="exercise reviewer subscription wait",
                target_url="pursers/tools/seat-kit",
                scope="interactive-no-send",
                required_fields=["test_output"],
                assigned_to=agent_ids["worker"],
            )
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            active["principal"] = principals["worker"]
            await call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )
            cursor = int(service.journal.read_after("pursers", 0)["latest_cursor"])
            active["principal"] = principals["reviewer"]

            parsed = generated._parser().parse_args(
                ["wait", "--submitted", "--since", str(cursor), "--timeout", "3"]
            )
            assert parsed.submitted is True
            async with Client(mcp, mode="2026-07-28", cache=None) as raw_client:
                adapter = LocalSubscriptionAdapter(
                    raw_client, service, agent_ids["reviewer"]
                )
                output = io.StringIO()

                async def run_wait() -> None:
                    with redirect_stdout(output):
                        await generated._cmd_wait(
                            adapter,
                            "pursers",
                            parsed.since,
                            parsed.timeout,
                            submitted=parsed.submitted,
                            poll_fallback=False,
                        )

                waiting = asyncio.create_task(run_wait())
                await asyncio.wait_for(adapter.ready.wait(), timeout=1)
                active["principal"] = principals["worker"]
                submitted = await call(
                    "ticket_submit",
                    agent_name="worker-agent",
                    ticket_id=ticket_id,
                    summary="ready for review",
                    notes="test_output: integration fixture",
                    files_changed=["tools/seat-kit/seat_new.py"],
                    stay_active=True,
                )
                assert submitted.structured_content["ticket"]["status"] == "submitted"
                await asyncio.wait_for(waiting, timeout=2)
                result = json.loads(output.getvalue())

                assert result["timed_out"] is False
                assert result["new_seq"] > cursor
                assert len(result["events"]) == 1
                assert result["events"][0]["ticket_id"] == ticket_id
                assert result["events"][0]["status_to"] == "submitted"
                assert adapter.ticket_list_calls == 0
                assert adapter.catchup_calls == 0
                assert adapter.events_calls[0]["only_mine"] is False
                assert adapter.events_calls[0]["touch"] is False
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_generated_main_real_listen_event_exits_zero_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pursers_client import BoardClient
    import pursers_client.client as client_module

    async def prepare():
        fixture = await build_local_central(tmp_path / "central", monkeypatch)
        (
            _central,
            _mcp,
            _service,
            principals,
            active,
            _agent_ids,
            call,
            _original_current_principal,
        ) = fixture
        active["principal"] = principals["worker"]
        await call("board_join", agent_name="event-actor")
        created = await call(
            "ticket_create",
            agent_name="event-actor",
            title="generated main early exit",
            description="exercise generated CLI over a real in-process listen",
            target_url="pursers/tools/seat-kit",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        return fixture, created.structured_content["ticket"]["ticket_id"]

    fixture, ticket_id = asyncio.run(prepare())
    central, mcp, _service, _principals, _active, _agent_ids, _call, original = fixture

    @asynccontextmanager
    async def http_context():
        yield object()

    class RealListenBoardClient(BoardClient):
        def _http(self):
            return http_context()

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_real_event")
    monkeypatch.setattr(generated, "_load_client", lambda: RealListenBoardClient)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_a, **_k: mcp
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")
    monkeypatch.setattr(sys, "argv", ["board.sh", "wait", "--timeout", "1"])

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = generated.main()
    finally:
        central.current_principal = original

    result = json.loads(stdout.getvalue())
    assert returncode == 0
    assert stderr.getvalue() == ""
    assert result["timed_out"] is False
    assert result["events"][0]["ticket_id"] == ticket_id
