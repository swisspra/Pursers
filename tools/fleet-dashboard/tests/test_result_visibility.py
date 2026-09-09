from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).parents[1] / "result_visibility.py"
sys.path.insert(0, str(MODULE.parent))
SPEC = importlib.util.spec_from_file_location("result_visibility_feature", MODULE)
assert SPEC and SPEC.loader
feature = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feature)
import fleet_dashboard as dashboard  # noqa: E402


def submission(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "submitted_at": "2030-01-01T10:00:00+00:00",
        "summary": "Implemented the result card",
        "files_changed": ["tools/feature.py", "docs/feature.md"],
        "notes": (
            "branch_and_commit: codex/TK-result @ "
            "0123456789abcdef0123456789abcdef01234567\nsecret details omitted"
        ),
    }
    value.update(overrides)
    return value


def ticket(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ticket_id": "TK-result",
        "status": "submitted",
        "submission_history": [submission()],
        "submitted_at": "2030-01-01T10:00:00+00:00",
    }
    value.update(overrides)
    return value


def test_projects_pending_submission_and_safe_artifact_references() -> None:
    result = feature.project_ticket_result(ticket())

    assert result == {
        "state": "pending",
        "summary": "Implemented the result card",
        "branch": "codex/TK-result",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "files_changed": ["tools/feature.py", "docs/feature.md"],
        "files_omitted": 0,
        "submitted_at": "2030-01-01T10:00:00+00:00",
        "review": {
            "verdict": None,
            "reviewer": None,
            "reviewed_at": None,
            "independent": False,
        },
    }


def test_distinguishes_approved_rejected_missing_and_failed() -> None:
    approved = ticket(
        status="closed",
        review_verdict="approve",
        reviewed_at="2030-01-01T11:00:00+00:00",
        submitted_by_principal_id="PR-worker",
        reviewed_by_principal_id="PR-reviewer",
        reviewed_by_agent_name="reviewer-1",
    )
    rejected = ticket(
        status="open",
        review_verdict="reject",
        reviewed_at="2030-01-01T11:00:00+00:00",
    )
    failed = ticket(status="terminated")

    assert feature.project_ticket_result(approved)["state"] == "approved"
    assert feature.project_ticket_result(approved)["review"]["independent"] is True
    assert feature.project_ticket_result(rejected)["state"] == "rejected"
    assert feature.project_ticket_result({"status": "open"})["state"] == "missing"
    assert feature.project_ticket_result(failed)["state"] == "failed"


def test_stale_review_does_not_relabel_new_submission() -> None:
    result = feature.project_ticket_result(
        ticket(
            review_verdict="reject",
            reviewed_at="2030-01-01T09:00:00+00:00",
            reviewed_by_agent_name="old-reviewer",
        )
    )

    assert result["state"] == "pending"
    assert result["review"] == {
        "verdict": None,
        "reviewer": None,
        "reviewed_at": None,
        "independent": False,
    }


def test_review_time_compares_absolute_instants_and_rejects_naive_values() -> None:
    same_instant = ticket(
        status="closed",
        review_verdict="approve",
        reviewed_at="2030-01-01T05:00:00-05:00",
    )
    naive = ticket(
        status="closed",
        review_verdict="approve",
        reviewed_at="2030-01-01T11:00:00",
    )

    assert feature.project_ticket_result(same_instant)["state"] == "approved"
    assert feature.project_ticket_result(naive)["state"] == "pending"


def test_omits_unsafe_or_unbounded_artifact_fields() -> None:
    many = [f"safe/{index}.txt" for index in range(60)]
    result = feature.project_ticket_result(
        ticket(
            submission_history=[
                submission(
                    files_changed=[
                        "/absolute",
                        "../escape",
                        "windows\\path",
                        "double//slash",
                        *many,
                    ],
                    notes=(
                        "branch_and_commit: ../../unsafe @ "
                        "0123456789abcdef0123456789abcdef01234567"
                    ),
                    summary="x" * 2_000,
                )
            ]
        )
    )

    assert result["branch"] is None
    assert result["commit"] is None
    assert result["files_changed"] == many[:50]
    assert result["files_omitted"] == 14
    assert len(result["summary"]) == feature.MAX_SUMMARY_CHARS


def test_never_projects_submission_or_review_notes() -> None:
    projected = feature.project_ticket_result(
        ticket(
            review_verdict="approve",
            reviewed_at="2030-01-01T11:00:00+00:00",
            review_notes="do not expose this",
        )
    )

    assert "notes" not in projected
    assert "review_notes" not in repr(projected)


def test_board_detail_registers_the_result_projection() -> None:
    row = dashboard._detail_ticket(ticket(title="Result ticket"))

    assert row["id"] == "TK-result"
    assert row["result"]["state"] == "pending"
    assert row["result"]["files_changed"] == [
        "tools/feature.py",
        "docs/feature.md",
    ]
