from __future__ import annotations

from contextlib import asynccontextmanager
import subprocess
from pathlib import Path

import pytest

from tools import stranded_approvals


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _ticket(ticket_id: str, title: str, branch: str, commit: str) -> dict[str, object]:
    return {
        "ticket_id": ticket_id,
        "title": title,
        "review_verdict": "approve",
        "submission_history": [
            {"notes": f"branch_and_commit: {branch} @ {commit}"}
        ],
    }


def test_synthetic_repo_classifies_all_required_cases(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    ancestor = _commit(repo, "base")

    _git(repo, "switch", "-c", "codex/TK-rebased")
    (repo / "rebased.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    rebased = _commit(repo, "original landed content")

    _git(repo, "switch", "main")
    (repo / "reference.txt").write_text("landed by another implementation\n", encoding="utf-8")
    _commit(repo, "Merge TK-stranded by reference")
    (repo / "rebased.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (repo / "carrier.txt").write_text("different commit\n", encoding="utf-8")
    _commit(repo, "rebased carrier")

    _git(repo, "switch", "-c", "codex/TK-stranded", ancestor)
    (repo / "stranded.txt").write_text("missing-one\nmissing-two\n", encoding="utf-8")
    stranded = _commit(repo, "stranded content")
    _git(repo, "switch", "main")

    tickets = [
        _ticket("TK-ancestor", "ancestor", "codex/TK-ancestor", ancestor),
        _ticket("TK-rebased", "rebased", "codex/TK-rebased", rebased),
        _ticket("TK-stranded", "stranded", "codex/TK-stranded", stranded),
        _ticket("TK-unlanded", "unlanded", "codex/TK-unlanded", stranded),
        {
            "ticket_id": "TK-no-sha",
            "title": "no sha",
            "review_verdict": "approve",
            "submission_history": [{"notes": "legacy submission metadata"}],
        },
        _ticket("TK-missing", "missing branch", "codex/TK-missing", "f" * 40),
    ]

    results = {
        result.ticket_id: result
        for result in stranded_approvals.audit_approvals(
            tickets, repo=repo, main_ref="main"
        )
    }

    assert results["TK-ancestor"].state == "LANDED_ANCESTOR"
    assert results["TK-rebased"].state == "LANDED_CONTENT"
    assert (results["TK-rebased"].matched_lines, results["TK-rebased"].added_lines) == (
        2,
        2,
    )
    assert results["TK-stranded"].state == "LANDED_BY_REFERENCE"
    assert results["TK-stranded"].matched_lines is None
    assert results["TK-stranded"].added_lines is None
    assert results["TK-unlanded"].state == "STRANDED"
    assert (
        results["TK-unlanded"].matched_lines,
        results["TK-unlanded"].added_lines,
    ) == (0, 2)
    assert results["TK-no-sha"].state == "UNVERIFIABLE"
    assert results["TK-missing"].state == "BRANCH_MISSING"


class _FakeClient:
    def __init__(self, tickets: list[dict[str, object]]) -> None:
        self.tickets = tickets
        self.calls: list[dict[str, object]] = []

    async def ticket_list(self, **arguments: object) -> dict[str, object]:
        self.calls.append(arguments)
        selected = self.tickets
        if "status" in arguments:
            selected = [
                ticket for ticket in selected if ticket["status"] == arguments["status"]
            ]
        if "assigned_to" in arguments:
            needle = str(arguments["assigned_to"]).casefold()
            selected = [
                ticket
                for ticket in selected
                if needle
                in {
                    str(ticket.get("assigned_to_agent_id") or "").casefold(),
                    str(ticket.get("assigned_to") or "").casefold(),
                }
            ]
        if "ticket_ids" in arguments:
            wanted = set(arguments["ticket_ids"])
            selected = [ticket for ticket in selected if ticket["ticket_id"] in wanted]
        limit = int(arguments["limit"])
        return {
            "tickets": selected[:limit],
            "count": len(selected[:limit]),
            "total_matching": len(selected),
        }

    async def board_status(self, **arguments: object) -> dict[str, object]:
        self.calls.append({"board_status": True, **arguments})
        agents = {
            (str(ticket.get("assigned_to_agent_id")), str(ticket.get("assigned_to")))
            for ticket in self.tickets
            if ticket.get("assigned_to_agent_id") or ticket.get("assigned_to")
        }
        return {
            "agents": [
                {"agent_id": agent_id, "agent_name": agent_name}
                for agent_id, agent_name in sorted(agents)
            ]
        }


@pytest.mark.anyio
async def test_ticket_reader_uses_archived_status_pages_and_bounded_full_batches() -> None:
    tickets = [
        {
            "ticket_id": "TK-one",
            "status": "closed",
            "title": "one",
            "review_verdict": "approve",
        },
        {"ticket_id": "TK-two", "status": "open", "title": "two"},
    ]
    client = _FakeClient(tickets)

    assert await stranded_approvals.fetch_all_tickets(client) == tickets
    status_calls = [call for call in client.calls if "status" in call]
    full_calls = [call for call in client.calls if call.get("view") == "full"]
    assert {call["status"] for call in status_calls} == set(
        stranded_approvals.TICKET_STATUSES
    )
    assert all(call["include_closed"] is True for call in client.calls)
    assert all(call["include_archived"] is True for call in client.calls)
    assert len(full_calls) == 1
    assert full_calls[0]["ticket_ids"] == ["TK-one", "TK-two"]


@pytest.mark.anyio
async def test_ticket_reader_splits_oversized_status_by_retired_assignees() -> None:
    tickets = [
        {
            "ticket_id": f"TK-{index:04d}",
            "status": "closed",
            "title": "ticket",
            "assigned_to_agent_id": "AI-a" if index % 2 == 0 else "AI-b",
            "assigned_to": "retired-a" if index % 2 == 0 else "retired-b",
        }
        for index in range(674)
    ]
    client = _FakeClient(tickets)

    result = await stranded_approvals.fetch_all_tickets(client)

    assert [ticket["ticket_id"] for ticket in result] == [
        ticket["ticket_id"] for ticket in tickets
    ]
    board_calls = [call for call in client.calls if call.get("board_status")]
    assert board_calls == [{"board_status": True, "include_retired": True}]
    assigned_calls = [call for call in client.calls if "assigned_to" in call]
    assert {call["assigned_to"] for call in assigned_calls} == {"AI-a", "AI-b"}


@pytest.mark.anyio
async def test_ticket_reader_fails_closed_when_split_still_misses_ids() -> None:
    tickets = [
        {"ticket_id": f"TK-{index:04d}", "status": "closed", "title": "ticket"}
        for index in range(501)
    ]
    with pytest.raises(stranded_approvals.AuditError, match="covered 500 of 501"):
        await stranded_approvals.fetch_all_tickets(_FakeClient(tickets))


@pytest.mark.anyio
async def test_ticket_reader_rejects_missing_id_from_full_batch() -> None:
    class MissingFullClient(_FakeClient):
        async def ticket_list(self, **arguments: object) -> dict[str, object]:
            result = await super().ticket_list(**arguments)
            if arguments.get("view") == "full" and result["tickets"]:
                rows = list(result["tickets"])[1:]
                return {"tickets": rows, "count": len(rows), "total_matching": len(rows)}
            return result

    tickets = [
        {"ticket_id": "TK-one", "status": "closed", "title": "one"},
        {"ticket_id": "TK-two", "status": "open", "title": "two"},
    ]
    with pytest.raises(stranded_approvals.AuditError, match="ticket batch mismatch"):
        await stranded_approvals.fetch_all_tickets(MissingFullClient(tickets))


@pytest.mark.anyio
async def test_live_reader_uses_dedicated_non_takeover_read_only_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2
    import pursers_client

    captured: dict[str, object] = {}

    @asynccontextmanager
    async def transport_context():
        yield object()

    class FakeBoardClient(_FakeClient):
        def __init__(self, *_args: object, **kwargs: object) -> None:
            super().__init__([])
            captured.update(kwargs)

        async def __aenter__(self) -> "FakeBoardClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(httpx2, "AsyncClient", lambda **_kwargs: transport_context())
    monkeypatch.setattr(pursers_client, "BoardClient", FakeBoardClient)
    args = stranded_approvals.build_parser().parse_args(
        ["--central-url", "https://central.example/mcp", "--board", "pursers"]
    )

    assert await stranded_approvals.read_live_tickets(args, "token") == []
    assert args.agent_name == stranded_approvals.DEFAULT_AUDIT_AGENT_NAME
    assert captured["agent_name"] == stranded_approvals.DEFAULT_AUDIT_AGENT_NAME
    assert captured["allow_takeover"] is False
    assert captured["capabilities"] == {
        "can_work": False,
        "can_review": False,
        "tier_max": 2,
        "max_parallel": 1,
    }


def test_takeover_requires_explicit_flag() -> None:
    args = stranded_approvals.build_parser().parse_args(
        [
            "--agent-name",
            "coordinator-1",
            "--allow-takeover",
        ]
    )
    assert args.agent_name == "coordinator-1"
    assert args.allow_takeover is True
