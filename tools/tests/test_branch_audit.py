from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from tools import branch_audit, prune_branches


def _branch(
    name: str,
    commit: str,
    *,
    merged: bool = False,
    date: str = "2026-09-01T00:00:00+00:00",
) -> branch_audit.RemoteBranch:
    return branch_audit.RemoteBranch(name, commit, date, merged)


def _ticket(ticket_id: str, status: str, board: str = "pursers") -> dict[str, str]:
    return {"ticket_id": ticket_id, "status": status, "board_id": board}


def test_classifier_accounts_for_four_buckets_and_protects_submitted_tip() -> None:
    submitted_sha = "e" * 40
    branches = [
        _branch("codex/TK-merged", "a" * 40, merged=True),
        _branch("fix/TK-closed", "b" * 40),
        _branch("codex/TK-live", "c" * 40),
        _branch("manual-name", "d" * 40),
        _branch("codex/TK-submitted", submitted_sha),
    ]
    tickets = {
        "TK-merged": [_ticket("TK-merged", "closed")],
        "TK-closed": [_ticket("TK-closed", "closed")],
        "TK-live": [_ticket("TK-live", "claimed")],
        "TK-submitted": [_ticket("TK-submitted", "submitted")],
    }
    report = branch_audit.build_report(
        branches,
        remote="origin",
        main_branch="main",
        tickets_by_id=tickets,
        submitted_branches={"codex/TK-submitted"},
        submitted_commits={submitted_sha},
        boards=["pursers"],
    )

    assert report["total_branches"] == 5
    assert report["category_counts"] == {
        "merged_into_main": 1,
        "unmerged_terminal_ticket": 1,
        "unmerged_live_ticket": 2,
        "no_resolvable_ticket": 1,
    }
    assert [row["branch"] for row in report["deletion_candidates"]["tier_a"]] == [
        "codex/TK-merged"
    ]
    assert [row["branch"] for row in report["deletion_candidates"]["tier_b"]] == [
        "fix/TK-closed"
    ]
    submitted = next(
        row for row in report["branches"] if row["branch"] == "codex/TK-submitted"
    )
    assert submitted["deletion_tier"] is None
    assert "submitted_branch_awaiting_review" in submitted["protected_reasons"]
    assert "submitted_commit_awaiting_review" in submitted["protected_reasons"]


def test_merged_branches_without_resolvable_ticket_are_protected() -> None:
    report = branch_audit.build_report(
        [
            _branch("manual-merged", "7" * 40, merged=True),
            _branch("codex/TK-unknown", "8" * 40, merged=True),
            _branch("codex/TK-ambiguous", "9" * 40, merged=True),
        ],
        remote="origin",
        main_branch="main",
        tickets_by_id={
            "TK-ambiguous": [
                _ticket("TK-ambiguous", "closed", "one"),
                _ticket("TK-ambiguous", "closed", "two"),
            ]
        },
        submitted_branches=set(),
        submitted_commits=set(),
        boards=["pursers"],
    )

    assert report["category_counts"] == {
        "merged_into_main": 0,
        "unmerged_terminal_ticket": 0,
        "unmerged_live_ticket": 0,
        "no_resolvable_ticket": 3,
    }
    assert report["deletion_candidates"]["tier_a"] == []
    rows = {row["branch"]: row for row in report["branches"]}
    assert all(row["deletion_tier"] is None for row in rows.values())
    assert rows["manual-merged"]["protected_reasons"] == [
        "unresolved_ticket:missing_ticket_id"
    ]
    assert rows["codex/TK-unknown"]["protected_reasons"] == [
        "unresolved_ticket:ticket_not_found"
    ]
    assert rows["codex/TK-ambiguous"]["protected_reasons"] == [
        "unresolved_ticket:ticket_id_is_ambiguous_across_boards"
    ]


def test_submitted_references_read_current_notes_not_rejected_history() -> None:
    current_sha = "1" * 40
    branches, commits = branch_audit.submitted_references(
        [
            {
                "status": "submitted",
                "notes": f"branch_and_commit: codex/TK-current@{current_sha}",
                "submission_history": [
                    {
                        "notes": (
                            "branch_and_commit: refs/heads/codex/TK-old "
                            f"@ {'2' * 40}"
                        )
                    }
                ],
            },
            {
                "status": "closed",
                "notes": f"branch_and_commit: codex/TK-done@{'3' * 40}",
            },
        ]
    )

    assert branches == {"codex/TK-current"}
    assert commits == {current_sha}


def test_merged_live_and_integration_branches_are_never_candidates() -> None:
    report = branch_audit.build_report(
        [
            _branch("codex/TK-live-merged", "4" * 40, merged=True),
            _branch("integration/TK-done", "5" * 40, merged=True),
        ],
        remote="origin",
        main_branch="main",
        tickets_by_id={
            "TK-live": [_ticket("TK-live", "open")],
            "TK-done": [_ticket("TK-done", "closed")],
        },
        submitted_branches=set(),
        submitted_commits=set(),
        boards=["pursers"],
    )

    assert report["deletion_candidates"]["tier_a"] == []
    rows = {row["branch"]: row for row in report["branches"]}
    assert rows["codex/TK-live-merged"]["protected_reasons"] == [
        "live_ticket_status:open"
    ]
    assert rows["integration/TK-done"]["protected_reasons"] == [
        "integration_branch"
    ]


def test_pruner_refuses_without_confirmation(tmp_path: Path, capsys) -> None:
    row = {
        "branch": "codex/TK-old",
        "commit": "a" * 40,
        "category": "merged_into_main",
        "deletion_tier": "A",
        "protected_reasons": [],
        "ticket_id": "TK-old",
    }
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "schema": branch_audit.SCHEMA,
                "remote": "origin",
                "main_branch": "main",
                "protections": {"submitted_branches": [], "submitted_commits": []},
                "deletion_candidates": {"tier_a": [row], "tier_b": []},
            }
        ),
        encoding="utf-8",
    )

    assert prune_branches.main([str(audit), "--tier", "A"]) == 2
    output = capsys.readouterr()
    assert "codex/TK-old" in output.out
    assert "refusing to delete" in output.err


def test_pruner_rejects_protected_default_in_candidate_data(tmp_path: Path) -> None:
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "schema": branch_audit.SCHEMA,
                "remote": "origin",
                "main_branch": "main",
                "protections": {"submitted_branches": [], "submitted_commits": []},
                "deletion_candidates": {
                    "tier_a": [
                        {
                            "branch": "main",
                            "commit": "a" * 40,
                            "category": "merged_into_main",
                            "deletion_tier": "A",
                            "protected_reasons": [],
                        }
                    ],
                    "tier_b": [],
                },
            }
        ),
        encoding="utf-8",
    )

    assert prune_branches.main([str(audit), "--tier", "A"]) == 1


def test_pruner_revalidates_closed_ticket_that_became_live(monkeypatch) -> None:
    row = {
        "branch": "codex/TK-reopened",
        "commit": "a" * 40,
        "category": "unmerged_terminal_ticket",
        "deletion_tier": "B",
        "ticket_id": "TK-reopened",
        "ticket_status": "closed",
        "ticket_board": "pursers",
    }

    async def fake_read_board_tickets(**_arguments):
        ticket = _ticket("TK-reopened", "submitted")
        return {"TK-reopened": [ticket]}, [ticket], ["pursers"]

    monkeypatch.setattr(prune_branches, "read_board_tickets", fake_read_board_tickets)

    try:
        asyncio.run(
            prune_branches.revalidate_board_state(
                [row],
                central_url="https://central.invalid/mcp",
                token="TOKEN_PLACEHOLDER",
                ca_file=None,
                board_ids=["pursers"],
            )
        )
    except prune_branches.PruneError as exc:
        assert "now live (submitted)" in str(exc)
    else:
        raise AssertionError("reopened ticket was not rejected")


def test_pruner_revalidates_current_submission_references(monkeypatch) -> None:
    commit = "b" * 40
    row = {
        "branch": "codex/TK-closed",
        "commit": commit,
        "ticket_id": "TK-closed",
        "ticket_board": "pursers",
    }

    async def fake_read_board_tickets(**_arguments):
        closed = _ticket("TK-closed", "closed")
        submitted = {
            **_ticket("TK-review", "submitted"),
            "notes": f"branch_and_commit: codex/TK-closed@{commit}",
        }
        return {"TK-closed": [closed]}, [submitted], ["pursers"]

    monkeypatch.setattr(prune_branches, "read_board_tickets", fake_read_board_tickets)

    try:
        asyncio.run(
            prune_branches.revalidate_board_state(
                [row],
                central_url="https://central.invalid/mcp",
                token="TOKEN_PLACEHOLDER",
                ca_file=None,
                board_ids=["pursers"],
            )
        )
    except prune_branches.PruneError as exc:
        assert "now awaiting review" in str(exc)
    else:
        raise AssertionError("current submission reference was not rejected")


def test_compare_and_delete_rejects_remote_ref_race(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Test"], check=True
    )
    tracked = source / "tracked.txt"
    tracked.write_text("audited\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "audited"],
        check=True,
        capture_output=True,
    )
    audited = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "branch", "codex/TK-race"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "origin", "codex/TK-race"],
        check=True,
        capture_output=True,
    )
    tracked.write_text("moved\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source), "commit", "-am", "moved"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "origin", "HEAD:refs/heads/codex/TK-race"],
        check=True,
        capture_output=True,
    )
    moved = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    try:
        prune_branches.execute(
            [{"branch": "codex/TK-race", "commit": audited}],
            repo=source,
            remote="origin",
        )
    except prune_branches.PruneError as exc:
        assert "compare-and-delete failed" in str(exc)
    else:
        raise AssertionError("moved remote ref was deleted")

    remote_tip = subprocess.run(
        ["git", "ls-remote", "--heads", str(remote), "refs/heads/codex/TK-race"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\t", 1)[0]
    assert remote_tip == moved
