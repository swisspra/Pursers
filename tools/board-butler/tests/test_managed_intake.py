"""Butler-managed intake: LLM-decided pulls, seat ceiling, paging, PR writeback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

import pytest
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_source_intake import NOW, SHA, SONAR_FIELDS, Model, butler


def _issue(n: int) -> dict[str, str]:
    return {
        "key": f"SONAR-{n}",
        "updatedAt": "r1",
        "message": f"Finding {n}",
        "details": "details",
        "url": f"https://sonar.invalid/SONAR-{n}",
        "project": "alpha",
    }


class PagedClient:
    """Fake MCP v2 client: a paged list tool and a PR tool without a call id."""

    protocol_version = "2026-07-28"

    def __init__(self, pages: dict[int, list[dict[str, str]]], calls: list) -> None:
        self.pages = pages
        self.calls = calls

    async def list_tools(self, **_kwargs: Any) -> Model:
        return Model(
            tools=[
                SimpleNamespace(
                    name="fetch",
                    description="List source items",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"page": {"type": "integer"}},
                    },
                ),
                SimpleNamespace(
                    name="pr_create",
                    description="Create a pull request",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["repositoryId", "sourceRefName", "targetRefName", "title"],
                        "properties": {
                            "repositoryId": {"type": "string"},
                            "project": {"type": "string"},
                            "sourceRefName": {"type": "string"},
                            "targetRefName": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                ),
            ],
            next_cursor=None,
        )

    async def list_resources(self, **_kwargs: Any) -> Model:
        return Model(resources=[], next_cursor=None)

    async def call_tool(self, name: str, arguments: dict[str, Any], **_kwargs: Any) -> Model:
        self.calls.append((name, dict(arguments)))
        if name == "fetch":
            return Model(structured_content={"issues": self.pages.get(arguments.get("page", 1), [])})
        return Model(structured_content={"pullRequestId": 7})


def _declaration() -> butler.ConnectorDeclaration:
    return butler.ConnectorDeclaration.from_mapping(
        {
            "connector_id": "connector:sonar",
            "enabled": True,
            "transport": "stdio",
            "protocol_revision": "2026-07-28",
            "endpoint_ref": "endpoint:sonar",
            "secret_ref": None,
            "tools": [
                {"name": "fetch", "effect": "read_only", "replay": "never",
                 "stable_call_id_field": None},
                {"name": "pr_create", "effect": "mutating", "replay": "never",
                 "stable_call_id_field": None},
            ],
            "resources": [],
            "risky_tools": ["pr_create"],
            "limits": {"timeout_ms": 1_000, "max_input_bytes": 4_096,
                       "max_output_bytes": 32_000, "max_concurrency": 2,
                       "calls_per_minute": 600},
        }
    )


def _runtime(declaration, client) -> butler.ConnectorRuntime:
    @contextlib.asynccontextmanager
    async def factory(_d, _e, _s) -> AsyncIterator[PagedClient]:
        yield client

    async def allow(_request):
        return butler.ConnectorPolicyDecision(True, "decision-1", "fixture_allow")

    return butler.ConnectorRuntime(
        board_id="pursers",
        project_id="registry",
        actor_id="board-butler",
        policy_digest_sha256=SHA,
        declaration=declaration,
        approved_connector_ids=[declaration.connector_id],
        endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint("fake-source"),
        secret_resolver=lambda _ref: "",
        persistence=butler.InMemoryConnectorPersistence(),
        policy_gate=allow,
        client_factory=factory,
    )


def _source(**extra: Any) -> dict[str, Any]:
    value = {
        "source_id": "sonar",
        "connector_id": "connector:sonar",
        "list_tool": "fetch",
        "fixed_args": {},
        "items_path": "issues",
        "field_map": dict(SONAR_FIELDS),
        "routing": {"project_map": {"alpha": "Alpha"}},
        "mode": "auto",
        "page_arg": "page",
        "max_pages": 5,
    }
    value.update(extra)
    return value


class Board:
    def __init__(self) -> None:
        self.state: str | None = None
        self.tickets: dict[str, dict[str, Any]] = {}

    async def read_state(self, _board_id: str):
        return None if self.state is None else {"state": {"value": self.state}}

    async def write_state(self, _board_id: str, value: str, _expected: str | None) -> None:
        self.state = value

    async def read_ticket(self, _board_id: str, ticket_id: str):
        return self.tickets.get(ticket_id)

    async def annotate(self, _board_id: str, ticket_id: str, text: str) -> None:
        self.tickets[ticket_id].setdefault("annotations", []).append({"text": text})

    def asks(self) -> list[dict[str, Any]]:
        return json.loads(self.state)["asks"] if self.state else []


def _poller(board: Board, runtime, sources, **kwargs) -> butler.SourceIntakePoller:
    return butler.SourceIntakePoller(
        sources=butler.parse_source_declarations(sources, {"connector:sonar": runtime.declaration}),
        runtimes={"connector:sonar": runtime},
        registry_projects={"Alpha": "board-a"},
        state_reader=board.read_state,
        state_writer=board.write_state,
        ticket_reader=board.read_ticket,
        ticket_annotator=board.annotate,
        **kwargs,
    )


def test_llm_decision_pulls_only_what_it_chose_and_pages_past_taken_items(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list = []
        client = PagedClient({1: [_issue(1), _issue(2)], 2: [_issue(3), _issue(4)]}, calls)
        runtime = _runtime(_declaration(), client)
        board = Board()
        contexts: list[Mapping[str, Any]] = []

        async def ceiling(in_flight):
            return 10 - sum(in_flight.values())

        async def decide(context):
            contexts.append(context)
            return {"pull": 2, "source_ids": ["sonar"], "reason": "idle seats"}

        index = butler.SourceIntakeIndex(tmp_path / "index.json")
        poller = _poller(board, runtime, [_source()], index=index, ceiling=ceiling, decide=decide)
        first = await poller.run_cycle(NOW)
        assert first["new_asks"] == 2
        assert [ask["source"]["external_id"] for ask in board.asks()] == ["SONAR-1", "SONAR-2"]
        assert contexts[0]["ceiling"] == 10
        assert oct(os.stat(tmp_path / "index.json").st_mode & 0o777) == "0o600"

        # Next cycle: page 1 is already taken, so the poller pages forward
        # without a Central call per taken item.
        board_reads = []
        original = board.read_ticket

        async def counting_read(board_id, ticket_id):
            board_reads.append(ticket_id)
            return await original(board_id, ticket_id)

        poller.ticket_reader = counting_read
        second = await poller.run_cycle(NOW + timedelta(minutes=1))
        assert second["new_asks"] == 2
        assert [ask["source"]["external_id"] for ask in board.asks()] == [
            "SONAR-1", "SONAR-2", "SONAR-3", "SONAR-4"
        ]
        # ticket reads: 2 in-flight writeback checks + 2 new items, none for taken ones
        assert len(board_reads) == 4
        assert contexts[1]["in_flight_by_source"] == {"sonar": 2}

        # Reloading the index from disk keeps the dedupe.
        reloaded = _poller(board, runtime, [_source()],
                           index=butler.SourceIntakeIndex(tmp_path / "index.json"),
                           ceiling=ceiling, decide=decide)
        third = await reloaded.run_cycle(NOW + timedelta(minutes=2))
        assert third["new_asks"] == 0

    asyncio.run(scenario())


def test_ceiling_bounds_the_model_and_failures_pull_nothing() -> None:
    async def scenario() -> None:
        calls: list = []
        client = PagedClient({1: [_issue(n) for n in range(1, 6)]}, calls)
        runtime = _runtime(_declaration(), client)

        async def ceiling(_in_flight):
            return 1

        async def greedy(_context):
            return {"pull": 50, "source_ids": ["sonar"], "reason": "all of it"}

        board = Board()
        result = await _poller(board, runtime, [_source()], ceiling=ceiling, decide=greedy).run_cycle(NOW)
        assert result["new_asks"] == 1 and result["decision"]["pull"] == 1

        async def broken(_context):
            raise RuntimeError("model down")

        calls.clear()
        board = Board()
        result = await _poller(board, runtime, [_source()], ceiling=ceiling, decide=broken).run_cycle(NOW)
        assert result["new_asks"] == 0 and calls == []
        assert result["findings"][0]["kind"] == "source-intake-decision-unavailable"

        async def full(_in_flight):
            return 0

        async def never_called(_context):
            raise AssertionError("no capacity means no model call")

        board = Board()
        result = await _poller(board, runtime, [_source()], ceiling=full, decide=never_called).run_cycle(NOW)
        assert result["new_asks"] == 0 and result["decision"]["reason"] == "no_capacity"

    asyncio.run(scenario())


@pytest.mark.parametrize("resident", [False, True])
def test_approved_ticket_opens_one_ado_pull_request_from_the_approved_branch(tmp_path: Path, resident: bool) -> None:
    async def scenario() -> None:
        calls: list = []
        client = PagedClient({1: [_issue(1)]}, calls)
        runtime = _runtime(_declaration(), client)
        board = Board()

        async def ceiling(in_flight):
            return 5 - sum(in_flight.values())

        async def decide(_context):
            return {"pull": 1, "reason": "go"}

        async def project(_board_id):
            return {
                "repository_url": "https://dev.azure.com/example-org/example-project/_git/example-backend",
                "integration_ref": "develop",
            }

        source = _source(
            writeback={
                "on": "approved",
                "tool": "pr_create",
                "arg_template": {
                    "repositoryId": "{repository_name}",
                    "project": "{repository_project}",
                    "sourceRefName": "refs/heads/{source_branch}",
                    "targetRefName": "refs/heads/{target_branch}",
                    "title": "{ticket_title}",
                    "description": "{link} ({ticket_id} @ {approved_sha})",
                },
            }
        )
        poller = _poller(board, runtime, [source], index=butler.SourceIntakeIndex(tmp_path / "i.json"),
                         ceiling=ceiling, decide=decide, project_reader=project)
        if resident:
            runtime.policy_gate = None  # Production loader has no blanket allow gate.
            backend = butler.CentralBackend(SimpleNamespace(
                _connector_runtimes=[runtime], _connector_sources=poller.sources,
                runtime_mode="active", source_intake_index_file=tmp_path / "resident.json",
            ), "unused")
            poller = backend.source_intake_poller
            poller.registry_projects = {"Alpha": "board-a"}
            poller.state_reader, poller.state_writer = board.read_state, board.write_state
            poller.ticket_reader, poller.ticket_annotator = board.read_ticket, board.annotate
            poller.ceiling, poller.decide, poller.project_reader = ceiling, decide, project
        await poller.run_cycle(NOW)
        ask = board.asks()[0]
        ticket_id = butler._source_ticket_id("board-a", ask["id"])
        sha = "b" * 40
        board.tickets[ticket_id] = {
            "status": "closed",
            "title": "Fix finding 1",
            "latest_verdict": {"verdict": "approve"},
            "latest_submission": {"notes": f"branch_and_commit: pursers/{ticket_id}@{sha}\n"},
            "description": "",
            "annotations": [],
        }
        calls.clear()
        first = await poller.run_cycle(NOW + timedelta(minutes=1))
        second = await poller.run_cycle(NOW + timedelta(minutes=2))
        pr_calls = [args for name, args in calls if name == "pr_create"]
        assert first["writebacks"] == 1 and second["writebacks"] == 0
        if resident:
            with pytest.raises(butler.ConnectorDenied):
                await runtime.call_tool("unapproved-pr", "pr_create", pr_calls[0])
        assert pr_calls == [
            {
                "repositoryId": "example-backend",
                "project": "example-project",
                "sourceRefName": f"refs/heads/pursers/{ticket_id}",
                "targetRefName": "refs/heads/develop",
                "title": "Fix finding 1",
                "description": f"https://sonar.invalid/SONAR-1 ({ticket_id} @ {sha})",
            }
        ]

    asyncio.run(scenario())


def test_failed_pull_request_is_not_retried_automatically(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list = []

        class FailingClient(PagedClient):
            async def call_tool(self, name, arguments, **kwargs):
                if name == "pr_create":
                    self.calls.append((name, dict(arguments)))
                    raise RuntimeError("ado down")
                return await super().call_tool(name, arguments, **kwargs)

        runtime = _runtime(_declaration(), FailingClient({1: [_issue(1)]}, calls))
        board = Board()

        async def ceiling(in_flight):
            return 5 - sum(in_flight.values())

        async def decide(_context):
            return {"pull": 1}

        source = _source(writeback={"on": "approved", "tool": "pr_create",
                                    "arg_template": {"repositoryId": "r", "sourceRefName": "s",
                                                     "targetRefName": "t", "title": "x"}})
        poller = _poller(board, runtime, [source], index=butler.SourceIntakeIndex(tmp_path / "i.json"),
                         ceiling=ceiling, decide=decide)
        await poller.run_cycle(NOW)
        ticket_id = butler._source_ticket_id("board-a", board.asks()[0]["id"])
        board.tickets[ticket_id] = {"status": "closed", "latest_verdict": {"verdict": "approve"},
                                    "description": "", "annotations": []}
        first = await poller.run_cycle(NOW + timedelta(minutes=1))
        await poller.run_cycle(NOW + timedelta(minutes=2))
        assert [name for name, _ in calls].count("pr_create") == 1
        assert any(f["kind"] == "source-intake-writeback-failed" for f in first["findings"])

    asyncio.run(scenario())


def test_repository_fields_parse_azure_devops_urls() -> None:
    fields = butler._repository_fields(
        {"repository_url": "https://org@dev.azure.com/example-org/new%20way%20of%20works/_git/example-service",
         "integration_ref": "main"}
    )
    assert fields["repository_org"] == "example-org"
    assert fields["repository_project"] == "new way of works"
    assert fields["repository_name"] == "example-service"
    assert butler._repository_fields({"repository_url": "https://github.com/x/y"})["repository_name"] == ""


def test_tool_only_connector_does_not_require_resource_support() -> None:
    async def scenario() -> None:
        class ToolOnlyClient(PagedClient):
            async def list_resources(self, **kwargs):
                raise RuntimeError("Method not found")
        calls = []
        runtime = _runtime(_declaration(), ToolOnlyClient({1: [_issue(1)]}, calls))
        result = await runtime.call_tool("tool-only-read", "fetch", {"page": 1})
        assert result.payload["structured_content"]["issues"][0]["key"] == "SONAR-1"
    asyncio.run(scenario())


def test_intake_board_load_counts_only_fresh_dispatchable_capacity():
    idle = {"capabilities": {"can_work": True, "can_review": False},
            "capabilities_explicit": True, "role": "worker", "status": "idle",
            "last_activity_at": NOW.isoformat()}
    reviewer = {**idle, "role": "reviewer",
                "capabilities": {"can_work": False, "can_review": True}}
    snapshot = {"tickets": [{"status": "open"}, {"status": "review"},
                            {"status": "closed"}, {"status": "canceled"}],
                "agents": [idle, reviewer, {**idle, "status": "busy"},
                    {**idle, "last_activity_at": (NOW-timedelta(minutes=6)).isoformat()},
                    {**idle, "readiness": {"reported": True, "dispatch_ready": False}},
                    {**idle, "lease_expires_at": (NOW+timedelta(minutes=1)).isoformat()},
                    {**idle, "role": "coordinator"}]}
    assert butler.source_intake_board_load(snapshot, NOW) == {
        "open": 1, "review": 1, "idle_workers": 1, "idle_reviewers": 1}
    assert butler.source_intake_board_load({}, NOW) == {
        "idle_workers": 0, "idle_reviewers": 0}


def test_intake_decision_cache_ignores_observation_time_but_tracks_work_changes(monkeypatch):
    async def scenario():
        import copy
        cache = butler.IntakeDecisionCache()
        runtime = butler.ProviderRuntime("https://model.invalid", "model-a", "secret")
        calls = []
        async def decide(_runtime, context):
            calls.append(copy.deepcopy(context))
            return {"pull": 0, "source_ids": [], "reason": "No work"}
        monkeypatch.setattr(butler, "decide_intake_with_provider", decide)
        context = {"ceiling": 15, "in_flight_by_source": {},
                   "board_load": {"board": {"idle_workers": 1, "idle_reviewers": 1}},
                   "sources": [{"source_id": "sonar", "open_issue_count": 0,
                                "observed_at": NOW.isoformat()}]}
        first = await cache.decide(runtime, context, NOW)
        first["pull"] = 99  # A caller cannot corrupt the cached response.
        for minute in range(1, 61):
            context["sources"][0]["observed_at"] = (NOW + timedelta(minutes=minute)).isoformat()
            assert (await cache.decide(runtime, context, NOW + timedelta(minutes=minute)))["pull"] == 0
        assert len(calls) == 1
        context["sources"][0]["open_issue_count"] = 2
        await cache.decide(runtime, context, NOW + timedelta(minutes=61))
        context["board_load"]["board"]["idle_workers"] = 2
        await cache.decide(runtime, context, NOW + timedelta(minutes=62))
        context["in_flight_by_source"]["sonar"] = 1
        await cache.decide(runtime, context, NOW + timedelta(minutes=63))
        context["ceiling"] = 14
        await cache.decide(runtime, context, NOW + timedelta(minutes=64))
        changed = butler.ProviderRuntime("https://model.invalid", "model-b", "secret")
        await cache.decide(changed, context, NOW + timedelta(minutes=65))
        assert len(calls) == 6
    asyncio.run(scenario())


def test_intake_decision_cache_backs_off_provider_failures(monkeypatch):
    async def scenario():
        cache = butler.IntakeDecisionCache()
        runtime = butler.ProviderRuntime("https://model.invalid", "model-a", "secret")
        calls = []
        async def decide(_runtime, context):
            calls.append(context)
            if len(calls) == 1:
                raise TimeoutError("provider unavailable")
            return {"pull": 1}
        monkeypatch.setattr(butler, "decide_intake_with_provider", decide)
        with pytest.raises(TimeoutError):
            await cache.decide(runtime, {"ceiling": 15}, NOW)
        with pytest.raises(butler.ButlerConfigError, match="retry is deferred"):
            await cache.decide(runtime, {"ceiling": 14}, NOW + timedelta(minutes=1))
        assert len(calls) == 1
        assert await cache.decide(runtime, {"ceiling": 14}, NOW + timedelta(minutes=15)) == {"pull": 1}
        assert len(calls) == 2
    asyncio.run(scenario())


def test_resident_reuses_intake_decisions_and_reports_model_calls(monkeypatch):
    async def scenario():
        backend = butler.CentralBackend(SimpleNamespace(), "opaque")
        calls = []
        async def config():
            return {}
        async def decide(_runtime, context):
            calls.append(context)
            return {"pull": 0, "reason": "No work"}
        monkeypatch.setattr(backend, "coordinator_config", config)
        monkeypatch.setattr(butler, "resolve_config", lambda *_a, **_kw: None)
        runtime = butler.ProviderRuntime("https://model.invalid", "model-a", "secret")
        monkeypatch.setattr(butler, "resolve_provider_runtime", lambda *_a, **_kw: runtime)
        monkeypatch.setattr(butler, "decide_intake_with_provider", decide)
        first = await backend._source_intake_decide({"ceiling": 15})
        second = await backend._source_intake_decide({"ceiling": 15})
        assert first["model_called"] is True
        assert second["model_called"] is False
        assert len(calls) == 1
        backend._source_board_load = {"board": {"idle_workers": 2}}
        assert (await backend._source_intake_decide({"ceiling": 15}))["model_called"] is True
        assert len(calls) == 2
    asyncio.run(scenario())
