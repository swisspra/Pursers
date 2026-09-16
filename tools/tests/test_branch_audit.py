from __future__ import annotations

import json
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
        "ticket_id": "TK-OLD",
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
