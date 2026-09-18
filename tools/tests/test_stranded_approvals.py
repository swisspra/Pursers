from __future__ import annotations

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
    assert results["TK-stranded"].state == "STRANDED"
    assert (results["TK-stranded"].matched_lines, results["TK-stranded"].added_lines) == (
        0,
        2,
    )
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
        if "ticket_ids" in arguments:
            wanted = set(arguments["ticket_ids"])
            selected = [ticket for ticket in selected if ticket["ticket_id"] in wanted]
        limit = int(arguments["limit"])
        return {
            "tickets": selected[:limit],
            "count": len(selected[:limit]),
            "total_matching": len(selected),
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
async def test_ticket_reader_rejects_truncated_status_page() -> None:
    tickets = [
        {"ticket_id": f"TK-{index}", "status": "closed", "title": "ticket"}
        for index in range(501)
    ]
    with pytest.raises(stranded_approvals.AuditError, match="page is incomplete"):
        await stranded_approvals.fetch_all_tickets(_FakeClient(tickets))
