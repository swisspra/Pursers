from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "case_study.py"
SPEC = importlib.util.spec_from_file_location("fleet_case_study", MODULE_PATH)
assert SPEC and SPEC.loader
case_study = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = case_study
SPEC.loader.exec_module(case_study)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _manifest(*, parallel_match: str = DIGEST_B) -> dict:
    def run(
        arm: str, board: str, prefix: str, start: str, finish: str, match: str
    ) -> dict:
        return {
            "run_id": f"private-{arm}-run",
            "pair_id": "private-pair-01",
            "arm": arm,
            "match_digest": match,
            "configured_concurrency": 1 if arm == "sequential" else 2,
            "tickets": [
                {
                    "board_id": board,
                    "ticket_id": f"TK-{prefix}-1",
                    "template_digest": DIGEST_A,
                },
                {
                    "board_id": board,
                    "ticket_id": f"TK-{prefix}-2",
                    "template_digest": DIGEST_B,
                },
            ],
            "events": [
                {"kind": "arm_released", "occurred_at": start},
                {
                    "kind": "ticket_eligible",
                    "board_id": board,
                    "ticket_id": f"TK-{prefix}-1",
                    "occurred_at": start,
                },
                {
                    "kind": "ticket_eligible",
                    "board_id": board,
                    "ticket_id": f"TK-{prefix}-2",
                    "occurred_at": start,
                },
                {"kind": "arm_finished", "occurred_at": finish},
            ],
        }

    return {
        "schema_version": 1,
        "study_id": "private-study-name",
        "manifest_digest": "c" * 64,
        "runs": [
            run(
                "sequential",
                "private-board-s",
                "S",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:20:00Z",
                DIGEST_B,
            ),
            run(
                "parallel",
                "private-board-p",
                "P",
                "2026-01-02T00:00:00Z",
                "2026-01-02T00:10:00Z",
                parallel_match,
            ),
        ],
    }


def _rows(*, truncate_parallel: bool = False) -> list[dict]:
    rows = []
    for board, prefix, base, parallel in (
        ("private-board-s", "S", "2026-01-01T00:", False),
        ("private-board-p", "P", "2026-01-02T00:", True),
    ):
        events = []
        tickets = []
        starts = ("01", "11") if not parallel else ("01", "01")
        submits = ("08", "18") if not parallel else ("05", "06")
        closes = ("10", "20") if not parallel else ("08", "10")
        for index in range(2):
            ticket_id = f"TK-{prefix}-{index + 1}"
            tickets.append({"ticket_id": ticket_id, "status": "closed"})
            events.extend(
                [
                    {
                        "seq": index * 4 + 1,
                        "kind": "ticket_claimed",
                        "ticket_id": ticket_id,
                        "occurred_at": f"{base}{starts[index]}:00Z",
                        "actor": f"AI-private-{index}",
                    },
                    {
                        "seq": index * 4 + 2,
                        "kind": "ticket_submitted",
                        "ticket_id": ticket_id,
                        "occurred_at": f"{base}{submits[index]}:00Z",
                    },
                    {
                        "seq": index * 4 + 3,
                        "kind": "review_started",
                        "ticket_id": ticket_id,
                        "occurred_at": f"{base}{submits[index]}:30Z",
                    },
                    {
                        "seq": index * 4 + 4,
                        "kind": "ticket_reviewed",
                        "ticket_id": ticket_id,
                        "review_verdict": "approved",
                        "occurred_at": f"{base}{closes[index]}:00Z",
                    },
                ]
            )
        rows.append(
            {
                "board_id": board,
                "snapshot": {"tickets": tickets},
                "events": events,
                "event_window_truncated": parallel and truncate_parallel,
                "event_resync_required": False,
            }
        )
    return rows


def test_aggregates_actual_concurrency_queue_lifecycle_and_matched_speedup() -> None:
    result = case_study.aggregate_case_study(_rows(), _manifest())

    assert result["status"] == "comparable"
    sequential, parallel = result["runs"]
    assert sequential["observed_peak_concurrency"] == 1
    assert parallel["observed_peak_concurrency"] == 2
    assert sequential["tickets"][0]["metrics"]["queue_time"] == {
        "status": "available",
        "seconds": 60.0,
    }
    assert parallel["tickets"][0]["metrics"]["review_work_time"] == {
        "status": "available",
        "seconds": 150.0,
    }
    assert result["comparisons"] == [
        {
            "pair_id": "private-pair-01",
            "status": "comparable",
            "reasons": [],
            "sequential_run_id": "private-sequential-run",
            "parallel_run_id": "private-parallel-run",
            "speedup": {"status": "available", "ratio": 2.0},
        }
    ]
    assert all(run["cost"]["status"] == "unavailable" for run in result["runs"])


def test_missing_review_start_alone_disables_speedup() -> None:
    rows = _rows()
    rows[0]["events"] = [
        event for event in rows[0]["events"] if event["kind"] != "review_started"
    ]
    result = case_study.aggregate_case_study(rows, _manifest())

    assert result["status"] == "incomparable"
    assert (
        result["runs"][0]["tickets"][0]["metrics"]["review_queue_time"]["status"]
        == "unavailable"
    )
    missing = result["runs"][0]["missing"]
    assert any("required review_started lifecycle event" in item for item in missing)
    assert any(
        "Sequential arm is incomplete" in item
        for item in result["comparisons"][0]["reasons"]
    )
    assert result["comparisons"][0]["speedup"]["status"] == "unavailable"


def test_missing_eligibility_alone_disables_speedup() -> None:
    manifest = _manifest()
    manifest["runs"][0]["events"] = [
        event
        for event in manifest["runs"][0]["events"]
        if event["kind"] != "ticket_eligible"
    ]

    result = case_study.aggregate_case_study(_rows(), manifest)

    assert result["status"] == "incomparable"
    missing = result["runs"][0]["missing"]
    assert any("required eligible lifecycle event" in item for item in missing)
    assert result["comparisons"][0]["speedup"]["status"] == "unavailable"


def test_duplicate_arm_makes_pair_incomparable() -> None:
    manifest = _manifest()
    duplicate = dict(manifest["runs"][0])
    duplicate["run_id"] = "private-sequential-run-duplicate"
    manifest["runs"].append(duplicate)

    result = case_study.aggregate_case_study(_rows(), manifest)

    comparison = result["comparisons"][0]
    assert comparison["status"] == "incomparable"
    assert "Pair requires exactly one sequential arm; found 2." in comparison["reasons"]
    assert comparison["speedup"]["status"] == "unavailable"


def test_mismatched_sequential_run_never_emits_speedup() -> None:
    result = case_study.aggregate_case_study(
        _rows(), _manifest(parallel_match="d" * 64)
    )

    assert result["comparisons"][0]["status"] == "incomparable"
    assert "Arm match digests differ." in result["comparisons"][0]["reasons"]
    assert result["comparisons"][0]["speedup"]["status"] == "unavailable"


def test_public_projection_uses_only_safe_aliases_and_coarse_aggregates() -> None:
    private = case_study.aggregate_case_study(_rows(), _manifest())
    public = case_study.project_public_case_study(private, b"x" * 32)
    encoded = json.dumps(public, sort_keys=True)

    assert public["mode"] == "public"
    assert public["comparisons"][0]["speedup"] == "at_least_twice_as_fast"
    assert public["comparisons"][0]["runs"][0]["work"] == "none"
    assert public["comparisons"][0]["runs"][0]["suppressed"] is True
    for forbidden in (
        "private-study-name",
        "private-pair-01",
        "private-board",
        "TK-S-1",
        "AI-private",
        "2026-01-",
        "/",
        DIGEST_A,
    ):
        assert forbidden not in encoded


def test_public_projection_omits_ticket_level_progress() -> None:
    private = case_study.aggregate_case_study(_rows(), _manifest())
    source_ticket = private["runs"][0]["tickets"][0]
    source_ticket["lease_expires_at"] = "2030-01-02T12:15:00Z"
    source_ticket["progress_freshness"] = "fresh"
    source_ticket["progress"] = {
        "low_percent": 35,
        "high_percent": 55,
        "confidence": "medium",
        "assessed_at": "2030-01-02T12:00:00Z",
        "evidence": "PRIVATE-PROGRESS-EVIDENCE",
    }

    public = case_study.project_public_case_study(private, b"x" * 32)
    encoded = json.dumps(public, sort_keys=True)

    for forbidden in (
        "progress",
        "progress_freshness",
        "lease_expires_at",
        "low_percent",
        "high_percent",
        "confidence",
        "assessed_at",
        "PRIVATE-PROGRESS-EVIDENCE",
    ):
        assert forbidden not in encoded


def test_manifest_rejects_non_source_ticket_references() -> None:
    manifest = _manifest()
    manifest["runs"][0]["tickets"] = ["TK-S-1"]
    with pytest.raises(case_study.CaseStudyError, match="board_id/ticket_id"):
        case_study.aggregate_case_study(_rows(), manifest)
