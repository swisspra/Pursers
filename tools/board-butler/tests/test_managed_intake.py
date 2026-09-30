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
        assert cache.failure == {"error_class": "TimeoutError"}
        with pytest.raises(butler.ButlerConfigError, match="retry is deferred"):
            await cache.decide(runtime, {"ceiling": 14}, NOW + timedelta(minutes=1))
        assert len(calls) == 1
        assert cache.failure == {"error_class": "TimeoutError"}
        assert await cache.decide(runtime, {"ceiling": 14}, NOW + timedelta(minutes=15)) == {"pull": 1}
        assert cache.failure == {}
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
        with_work = {"ceiling": 15, "sources": [{"open_issue_count": 1}]}
        assert (await backend._source_intake_decide(with_work))["model_called"] is True
        assert len(calls) == 3
        empty = {"ceiling": 15, "sources": [{"open_issue_count": 0}]}
        assert (await backend._source_intake_decide(empty))["model_called"] is False
        assert len(calls) == 3
        assert (await backend._source_intake_decide(with_work))["model_called"] is True
        assert len(calls) == 4
    asyncio.run(scenario())


@pytest.mark.parametrize("sources", [
    [{"source_id": "sonar", "open_issue_count": 0}],
    [{"source_id": "a", "open_issue_count": 0}, {"source_id": "b", "open_issue_count": 0}],
])
def test_resident_skips_model_when_all_sources_are_confirmed_empty(monkeypatch, sources):
    async def scenario():
        backend = butler.CentralBackend(SimpleNamespace(), "opaque")
        async def unexpected_config():
            pytest.fail("Confirmed empty sources must not resolve or call a model")
        monkeypatch.setattr(backend, "coordinator_config", unexpected_config)
        result = await backend._source_intake_decide({"ceiling": 15, "sources": sources})
        assert result == {"pull": 0, "source_ids": [], "reason": "no_open_issues", "model_called": False}
    asyncio.run(scenario())


@pytest.mark.parametrize("sources", [
    [], [{"open_issue_count": None}], [{"open_issue_count": False}],
    [{"open_issue_count": -1}], [{"open_issue_count": 0}, {"open_issue_count": 2}],
    [{"open_issue_count": 0, "observation_error": "TimeoutError"}],
])
def test_resident_does_not_treat_unknown_or_nonempty_sources_as_empty(monkeypatch, sources):
    async def scenario():
        backend = butler.CentralBackend(SimpleNamespace(), "opaque")
        async def config():
            return {}
        calls = []
        async def decide(_runtime, context):
            calls.append(context)
            return {"pull": 0}
        monkeypatch.setattr(backend, "coordinator_config", config)
        monkeypatch.setattr(butler, "resolve_config", lambda *_a, **_kw: None)
        runtime = butler.ProviderRuntime("https://model.invalid", "model", "secret")
        monkeypatch.setattr(butler, "resolve_provider_runtime", lambda *_a, **_kw: runtime)
        monkeypatch.setattr(butler, "decide_intake_with_provider", decide)
        result = await backend._source_intake_decide({"ceiling": 15, "sources": sources})
        assert result["model_called"] is True
        assert len(calls) == 1
    asyncio.run(scenario())


def test_revision_update_cannot_replay_uncertain_delivery(tmp_path):
    async def scenario():
        calls = []
        issue = _issue(1)
        issue['updatedAt'] = 'r2'
        runtime = _runtime(_declaration(), PagedClient({1: [issue]}, calls))
        board = Board()
        ticket_id = butler._source_ticket_id('board-a', butler._source_ask_id('board-a', 'sonar', 'SONAR-1'))
        board.tickets[ticket_id] = {'status': 'closed', 'review_verdict': 'approve'}
        index = butler.SourceIntakeIndex(tmp_path / 'index.json')
        index.put('sonar', 'SONAR-1', {'source_id': 'sonar', 'external_id': 'SONAR-1',
                  'revision': 'r1', 'board_id': 'board-a', 'ticket_id': ticket_id, 'status': 'delivering'})
        index.save()
        source = _source(writeback={'on': 'approved', 'tool': 'pr_create', 'arg_template': {
            'repositoryId': 'r', 'sourceRefName': 's', 'targetRefName': 't', 'title': 'x'}})
        poller = _poller(board, runtime, [source], index=butler.SourceIntakeIndex(index.path))
        await poller.run_cycle(NOW)
        assert not [name for name, _ in calls if name == 'pr_create']
        assert butler.SourceIntakeIndex(index.path).get('sonar', 'SONAR-1')['status'] == 'delivering'
    asyncio.run(scenario())


def test_removed_indexed_source_requires_explicit_migration(tmp_path):
    index = butler.SourceIntakeIndex(tmp_path / 'index.json')
    index.put('old-source', 'issue', {'source_id': 'old-source', 'status': 'asked'})
    with pytest.raises(butler.ConnectorConfigError, match='migration'):
        _poller(Board(), _runtime(_declaration(), PagedClient({}, [])), [_source()], index=index)


def _delivery_setup(tmp_path, *, remote_sha='b'*40, mismatch=False, annotate_fails=False):
    calls = []
    class DeliveryClient(PagedClient):
        async def list_tools(self, **kwargs):
            result = await super().list_tools(**kwargs)
            result.tools.append(SimpleNamespace(name='refs', input_schema={
                'type': 'object', 'properties': {'project': {'type': 'string'},
                'repositoryId': {'type': 'string'}, 'filter': {'type': 'string'}}}))
            return result
        async def call_tool(self, name, arguments, **kwargs):
            if name == 'refs':
                self.calls.append((name, dict(arguments)))
                return Model(structured_content={'value': [{'name': 'refs/heads/pursers/TK-test', 'objectId': remote_sha}]})
            return await super().call_tool(name, arguments, **kwargs)
    declaration = _declaration()
    from dataclasses import replace
    declaration = replace(declaration, tools=(*declaration.tools, replace(declaration.tools[0], name='refs')))
    runtime = _runtime(declaration, DeliveryClient({}, calls))
    board = Board()
    board.tickets['TK-test'] = {'status': 'closed', 'review_verdict': 'approve',
        'latest_submission': {'notes': 'branch_and_commit: pursers/TK-test@'+'b'*40}}
    if annotate_fails:
        async def annotate(*_):
            raise RuntimeError('annotation unavailable')
        board.annotate = annotate
    async def project(_):
        return {'repository_url': 'https://dev.azure.com/example-org/example-project/_git/example-repo', 'integration_ref': 'main'}
    source = _source(writeback={'on': 'approved', 'tool': 'pr_create', 'arg_template': {
        'project': 'wrong-project' if mismatch else '{repository_project}',
        'repositoryId': '{repository_name}', 'sourceRefName': 'refs/heads/{source_branch}',
        'targetRefName': 'refs/heads/{target_branch}', 'title': '{ticket_id}'},
        'preflight': {'read_tool': 'refs', 'arg_template': {'project': '{repository_project}',
            'repositoryId': '{repository_name}', 'filter': 'heads/{source_branch}'},
            'refs_path': 'value', 'name_path': 'name', 'sha_path': 'objectId'}})
    index = butler.SourceIntakeIndex(tmp_path / 'index.json')
    index.put('sonar', 'one', {'source_id': 'sonar', 'external_id': 'one', 'revision': 'r1',
              'board_id': 'board-a', 'ticket_id': 'TK-test', 'status': 'asked'})
    index.save()
    poller = _poller(board, runtime, [source], index=index, project_reader=project)
    return poller, calls


@pytest.mark.parametrize('remote_sha,mismatch', [('c'*40, False), ('b'*40, True)])
def test_delivery_preflight_blocks_unapproved_remote_branch_or_target(tmp_path, remote_sha, mismatch):
    async def scenario():
        poller, calls = _delivery_setup(tmp_path, remote_sha=remote_sha, mismatch=mismatch)
        result = await poller.run_cycle(NOW)
        assert not [name for name, _ in calls if name == 'pr_create']
        assert any(f['kind'] == 'source-intake-writeback-failed' for f in result['findings'])
    asyncio.run(scenario())


def test_delivery_annotation_failure_preserves_attempt_across_restart(tmp_path):
    async def scenario():
        poller, calls = _delivery_setup(tmp_path, annotate_fails=True)
        await poller.run_cycle(NOW)
        poller.index = butler.SourceIntakeIndex(poller.index.path)
        await poller.run_cycle(NOW + timedelta(minutes=1))
        assert [name for name, _ in calls].count('pr_create') == 1
        assert poller.index.get('sonar', 'one')['status'] == 'delivering'
    asyncio.run(scenario())


def test_delivery_preflight_accepts_exact_approved_head(tmp_path):
    async def scenario():
        poller, calls = _delivery_setup(tmp_path)
        assert (await poller.run_cycle(NOW))['writebacks'] == 1
        assert [name for name, _ in calls].count('pr_create') == 1
    asyncio.run(scenario())


def test_intake_audit_correlates_real_provider_id_without_prompt_or_secret(monkeypatch):
    async def scenario():
        backend=butler.CentralBackend(SimpleNamespace(),'opaque')
        runtime=butler.ProviderRuntime('https://model.invalid','model-a','private-test-key',draft_protocol='openai_chat_completions_v1')
        async def config():return {}
        async def post(*args,**kwargs):
            return {'id':'chatcmpl-fixture-123','usage':{'prompt_tokens':212,'completion_tokens':46},
                    'choices':[{'message':{'content':'{"pull":0,"reason":"wait"}'}}]}
        monkeypatch.setattr(backend,'coordinator_config',config)
        monkeypatch.setattr(butler,'resolve_config',lambda *a,**k:None)
        monkeypatch.setattr(butler,'resolve_provider_runtime',lambda *a,**k:runtime)
        monkeypatch.setattr(butler,'_post_provider_json',post)
        first=await backend._source_intake_decide({'ceiling':15})
        second=await backend._source_intake_decide({'ceiling':15})
        assert first['provider_response_id']=='chatcmpl-fixture-123'
        assert first['model_called'] and not first['cache_reused']
        assert not second['model_called'] and second['cache_reused']
        assert 'provider_response_id' not in second
        assert 'private-test-key' not in json.dumps([first,second])
    asyncio.run(scenario())


def test_preflight_rejects_same_named_repository_in_another_organization(tmp_path):
    from dataclasses import replace
    async def scenario():
        poller,calls=_delivery_setup(tmp_path)
        source=poller.sources[0]
        policy={**source.writeback.preflight,'repository_url_path':'repository.remoteUrl'}
        poller.sources=(replace(source,writeback=replace(source.writeback,preflight=policy)),)
        runtime=poller.runtimes[source.connector_id]
        original=runtime.call_tool
        async def call(operation,tool,arguments):
            if tool=='refs':
                return SimpleNamespace(payload={'structured_content':{'repository':{'remoteUrl':'https://dev.azure.com/another/example-project/_git/example-repo'},
                    'value':[{'name':'refs/heads/pursers/TK-test','objectId':'b'*40}]}})
            return await original(operation,tool,arguments)
        runtime.call_tool=call
        result=await poller.run_cycle(NOW)
        assert not [name for name,_ in calls if name=='pr_create']
        assert result['findings']
    asyncio.run(scenario())


def test_repository_identity_accepts_ado_organization_username_only():
    expected='https://dev.azure.com/example-org/example%20project/_git/repo'
    observed='https://example-org@dev.azure.com/example-org/example%20project/_git/repo'
    assert butler._repository_identity(expected)==butler._repository_identity(observed)
    with pytest.raises(butler.ConnectorDenied):
        butler._repository_identity('https://user:password@dev.azure.com/example-org/project/_git/repo')


def test_resident_project_reader_preserves_nondefault_delivery_branch():
    row = {"board_id": "alpha", "work_dir": "/repo/alpha", "status": "active",
           "repository_url": "https://dev.azure.com/example/team/_git/backend",
           "integration_ref": "dev"}
    class Client:
        async def board_state_get(self, key):
            assert key == "project_registry"
            return {"state": {"value": json.dumps({"schema_version": 1, "projects": {"alpha": row}})}}
    @contextlib.asynccontextmanager
    async def client_for_board(board_id):
        assert board_id == "home"
        yield Client()
    backend = object.__new__(butler.CentralBackend)
    backend.args = SimpleNamespace(home_board="home")
    backend._client_for_board = client_for_board
    result = asyncio.run(backend._source_project_reader("alpha"))
    assert butler._repository_fields(result)["target_branch"] == "dev"


def test_resident_reports_safe_provider_failure_through_backoff(monkeypatch):
    async def scenario():
        backend = butler.CentralBackend(SimpleNamespace(), "opaque")
        async def config():
            return {}
        async def fail(*_args):
            raise butler.urllib.error.HTTPError(
                "https://model.invalid/private", 429, "credential=do-not-log", {}, None
            )
        monkeypatch.setattr(backend, "coordinator_config", config)
        monkeypatch.setattr(butler, "resolve_config", lambda *_a, **_kw: None)
        runtime = butler.ProviderRuntime("https://model.invalid", "model", "secret")
        monkeypatch.setattr(butler, "resolve_provider_runtime", lambda *_a, **_kw: runtime)
        monkeypatch.setattr(butler, "decide_intake_with_provider", fail)
        for expected_called in (True, False):
            result = await backend._source_intake_decide({"ceiling": 15})
            assert result["pull"] == 0
            assert result["model_called"] is expected_called
            assert result["error_class"] == "HTTPError"
            assert result["http_status"] == 429
            assert "do-not-log" not in json.dumps(result)
            assert "model.invalid" not in json.dumps(result)
    asyncio.run(scenario())


def test_intake_cache_reports_truncated_json_without_response_text(monkeypatch):
    async def scenario():
        async def post(*args, **kwargs):
            return {"choices": [{"finish_reason": "length", "message": {"content": '{"pull":'}}]}
        monkeypatch.setattr(butler, "_post_provider_json", post)
        runtime = butler.ProviderRuntime("https://model.invalid", "model", "secret", draft_protocol="openai_chat_completions_v1")
        cache = butler.IntakeDecisionCache()
        with pytest.raises(json.JSONDecodeError):
            await cache.decide(runtime, {"ceiling": 15}, NOW)
        assert cache.failure == {"error_class": "JSONDecodeError", "response_chars": 8, "response_truncated": True}
    asyncio.run(scenario())


def test_intake_request_reserves_reasoning_and_json_output_budget(monkeypatch):
    async def scenario():
        async def post(_runtime, body, **_kwargs):
            request = json.loads(body)
            assert request["max_tokens"] == 1600
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"pull": 1, "source_ids": ["source"]}'}}],
                    "usage": {"prompt_tokens": 289, "completion_tokens": 207, "total_tokens": 496,
                              "completion_tokens_details": {"reasoning_tokens": 162}, "private": "not-public"}}
        monkeypatch.setattr(butler, "_post_provider_json", post)
        runtime = butler.ProviderRuntime("https://model.invalid", "model", "secret", draft_protocol="openai_chat_completions_v1")
        cache = butler.IntakeDecisionCache()
        assert (await cache.decide(runtime, {"ceiling": 1}, NOW))["pull"] == 1
        assert cache.evidence["provider_usage"] == {"prompt_tokens": 289, "completion_tokens": 207,
                                                    "total_tokens": 496, "reasoning_tokens": 162}
        assert (await cache.decide(runtime, {"ceiling": 1}, NOW))["pull"] == 1
        assert not cache.model_called
        assert cache.evidence == {}  # Cached reads are not new usage.
    asyncio.run(scenario())
