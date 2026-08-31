from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "coordinator.py"
SPEC = importlib.util.spec_from_file_location("coordinator", MODULE_PATH)
assert SPEC and SPEC.loader
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)

NOW = datetime(2030, 1, 8, 12, tzinfo=timezone.utc)


def ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(181, "healthy"), (180, "at-risk"), (-1, "expired"), (-600, "abandoned")],
)
def test_lease_threshold_classification_matrix(remaining: int, expected: str) -> None:
    ticket = {"lease_expires_at": (NOW + timedelta(seconds=remaining)).isoformat()}
    assert coordinator.classify_lease(ticket, 900, NOW, coordinator.Thresholds()) == expected


@pytest.mark.parametrize(
    ("priority", "age", "stage"),
    [
        ("medium", 1_799, 0),
        ("medium", 1_800, 1),
        ("medium", 3_600, 2),
        ("critical", 599, 0),
        ("critical", 600, 1),
        ("critical", 1_200, 2),
    ],
)
def test_escalation_ladder_timing(priority: str, age: int, stage: int) -> None:
    ticket = {"status": "open", "priority": priority, "created_at": ago(age)}
    assert coordinator.starvation_stage(ticket, NOW, coordinator.Thresholds()) == stage


def test_stage_two_names_least_loaded_live_assignee() -> None:
    snapshot = {
        "board": {"claim_ttl_s": 900},
        "agents": [
            {"agent_id": "AI-busy", "agent_name": "worker-a", "last_activity_at": ago(10), "status": "working", "membership_role": "member"},
            {"agent_id": "AI-free", "agent_name": "worker-b", "last_activity_at": ago(20), "status": "active", "membership_role": "member"},
        ],
        "tickets": [{"ticket_id": "TK-old", "status": "open", "priority": "medium", "created_at": ago(3_601)}],
    }
    finding = coordinator.ticket_findings("board-a", snapshot, NOW)[0]
    assert finding["escalation_stage"] == 2
    assert finding["would_assign_to_agent_id"] == "AI-free"


@pytest.mark.parametrize(
    "kind",
    [
        "starved",
        "claim-health",
        "snapshot-truncated",
        "repeat-abandoner",
        "repeat-abandoner-history-incomplete",
        "closed-but-unmerged",
        "unverifiable-commit",
        "integration-check-unavailable",
        "privacy-scan-unavailable",
        "privacy-leak-suspect",
        "privacy-scan-truncated",
        "would_nudge",
        "would_assign",
        "nudge",
        "assign",
        "mutation_failed",
        "coordinator_circuit_open",
        "review-backlog",
        "board-degraded",
    ],
)
def test_every_finding_kind_has_bounded_evidence_and_safe_next_action(
    kind: str,
) -> None:
    finding = coordinator._finding(
        kind,
        "warn",
        "board-a",
        "Deterministic finding.",
        ticket_id="TK-example",
        observed=4_000,
        threshold=1_800,
        seats=["reviewer-a", "reviewer-b"],
        oversized="x" * 1_000,
    )

    assert 0 < len(finding["evidence"]) <= coordinator.MAX_EVIDENCE_CHARS
    assert "TK-example" in finding["evidence"]
    assert "observed=4000" in finding["evidence"]
    assert "threshold=1800" in finding["evidence"]
    assert "\n" not in finding["next_action"]
    assert finding["next_action"].endswith(".")


@pytest.mark.parametrize(
    ("age", "level"),
    [(1_799, None), (1_800, "warn"), (3_599, "warn"), (3_600, "critical")],
)
def test_review_backlog_threshold_and_two_x_escalation(
    age: int, level: str | None
) -> None:
    snapshot = {
        "board": {"claim_ttl_s": 900},
        "agents": [
            {
                "agent_id": "AI-review-b",
                "agent_name": "reviewer-b",
                "role": "reviewer",
            },
            {
                "agent_id": "AI-review-a",
                "agent_name": "reviewer-a",
                "membership_role": "reviewer",
            },
            {
                "agent_id": "AI-worker",
                "agent_name": "worker",
                "role": "builder",
            },
        ],
        "tickets": [
            {
                "ticket_id": "TK-review",
                "status": "submitted",
                "submitted_at": ago(age),
            }
        ],
    }

    findings = [
        item
        for item in coordinator.ticket_findings("board-a", snapshot, NOW)
        if item["kind"] == "review-backlog"
    ]

    if level is None:
        assert findings == []
        return
    assert findings[0]["level"] == level
    assert findings[0]["observed_age_seconds"] == age
    assert findings[0]["threshold_seconds"] == 1_800
    assert findings[0]["reviewer_seats"] == ["reviewer-a", "reviewer-b"]
    assert "reviewer_seats" in findings[0]["evidence"]


def test_repeat_abandoner_counts_three_recent_drops_on_one_ticket() -> None:
    ticket = {
        "ticket_id": "TK-repeat",
        "status": "open",
        "created_at": ago(10),
        "abandoned_count": 3,
        "last_abandoned_by": "AI-repeat",
        "last_abandoned_at": ago(10),
    }
    previous = {
        "drop_counters": {"TK-repeat": 2},
        "drop_history": [
            {
                "ticket_id": "TK-repeat",
                "holder_agent_id": "AI-repeat",
                "observed_at": ago(30),
                "count": 1,
            },
            {
                "ticket_id": "TK-repeat",
                "holder_agent_id": "AI-repeat",
                "observed_at": ago(20),
                "count": 1,
            },
        ],
    }
    findings, counters, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], previous, NOW
    )
    repeats = [item for item in findings if item["kind"] == "repeat-abandoner"]
    assert repeats == [
        {
            "kind": "repeat-abandoner",
            "level": "warn",
            "board_id": "board-a",
            "message": "A seat reached the repeated dropped-claim threshold within the proven reporting window.",
            "evidence": "board_id=board-a; holder_agent_id=AI-repeat; dropped_claims=3; window_days=7",
            "next_action": "Review the named seat on board-a before assigning more work to it.",
            "holder_agent_id": "AI-repeat",
            "dropped_claims": 3,
            "window_days": 7,
        }
    ]
    assert counters == {"TK-repeat": 3}
    assert len(history) == 3
    assert all(item["count"] == 1 for item in history)
    assert uncertainty == []


def test_repeat_abandoner_first_snapshot_reports_unproven_window() -> None:
    ticket = {
        "ticket_id": "TK-baseline",
        "abandoned_count": 3,
        "last_abandoned_by": "AI-unknown-history",
        "last_abandoned_at": ago(10),
    }
    findings, counters, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {}, NOW
    )
    assert history == [
        {
            "ticket_id": "TK-baseline",
            "holder_agent_id": "AI-unknown-history",
            "observed_at": ago(10),
            "count": 1,
        }
    ]
    assert uncertainty == [
        {
            "ticket_id": "TK-baseline",
            "observed_at": NOW.isoformat(),
            "count": 2,
        }
    ]
    assert [item["kind"] for item in findings] == [
        "repeat-abandoner-history-incomplete"
    ]

    # The limitation remains active on an unchanged second cycle.
    previous = {
        "drop_counters": counters,
        "drop_history": history,
        "drop_uncertainty": uncertainty,
    }
    findings2, _, _, uncertainty2 = coordinator.update_drop_evidence(
        "board-a", [ticket], previous, NOW + timedelta(seconds=60)
    )
    assert [item["kind"] for item in findings2] == [
        "repeat-abandoner-history-incomplete"
    ]
    assert uncertainty2 == uncertainty


def test_baseline_two_then_three_keeps_incomplete_history() -> None:
    ticket = {
        "ticket_id": "TK-two-cycle",
        "abandoned_count": 2,
        "last_abandoned_by": "AI-latest",
        "last_abandoned_at": ago(20),
    }
    _, counters, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {}, NOW
    )
    ticket["abandoned_count"] = 3
    ticket["last_abandoned_at"] = ago(10)
    findings, _, history2, uncertainty2 = coordinator.update_drop_evidence(
        "board-a",
        [ticket],
        {
            "drop_counters": counters,
            "drop_history": history,
            "drop_uncertainty": uncertainty,
        },
        NOW + timedelta(seconds=60),
    )
    assert [item["kind"] for item in findings] == [
        "repeat-abandoner-history-incomplete"
    ]
    assert len(history2) == 2 and all(item["count"] == 1 for item in history2)
    assert uncertainty2[0]["count"] == 1


def test_multi_count_delta_attributes_only_latest_proven_drop() -> None:
    ticket = {
        "ticket_id": "TK-multi",
        "abandoned_count": 3,
        "last_abandoned_by": "AI-latest-only",
        "last_abandoned_at": ago(10),
    }
    findings, _, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {"drop_counters": {"TK-multi": 0}}, NOW
    )
    assert history == [
        {
            "ticket_id": "TK-multi",
            "holder_agent_id": "AI-latest-only",
            "observed_at": ago(10),
            "count": 1,
        }
    ]
    assert uncertainty[0]["count"] == 2
    assert [item["kind"] for item in findings] == [
        "repeat-abandoner-history-incomplete"
    ]


def test_baseline_uncertainty_expires_after_full_observation_window() -> None:
    ticket = {"ticket_id": "TK-covered", "abandoned_count": 3}
    previous = {
        "drop_counters": {"TK-covered": 3},
        "drop_uncertainty": [
            {
                "ticket_id": "TK-covered",
                "observed_at": NOW.isoformat(),
                "count": 3,
            }
        ],
    }
    findings, _, history, uncertainty = coordinator.update_drop_evidence(
        "board-a",
        [ticket],
        previous,
        NOW + timedelta(days=7, seconds=1),
    )
    assert findings == []
    assert history == []
    assert uncertainty == []


def test_old_latest_baseline_never_enters_seven_day_evidence() -> None:
    ticket = {
        "ticket_id": "TK-old-baseline",
        "abandoned_count": 3,
        "last_abandoned_by": "AI-old",
        "last_abandoned_at": (NOW - timedelta(days=7, seconds=1)).isoformat(),
    }
    findings, counters, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {}, NOW
    )
    assert findings == []
    assert counters == {"TK-old-baseline": 3}
    assert history == []
    assert uncertainty == []

    findings2, _, history2, uncertainty2 = coordinator.update_drop_evidence(
        "board-a",
        [ticket],
        {
            "drop_counters": counters,
            "drop_history": history,
            "drop_uncertainty": uncertainty,
        },
        NOW + timedelta(seconds=60),
    )
    assert findings2 == []
    assert history2 == []
    assert uncertainty2 == []


@pytest.mark.parametrize("timestamp", [None, "not-a-time"])
def test_unknown_baseline_time_remains_conservative(timestamp: str | None) -> None:
    ticket = {
        "ticket_id": "TK-unknown-time",
        "abandoned_count": 3,
        "last_abandoned_at": timestamp,
    }
    findings, _, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {}, NOW
    )
    assert history == []
    assert uncertainty[0]["count"] == 3
    assert [item["kind"] for item in findings] == [
        "repeat-abandoner-history-incomplete"
    ]


def test_three_baseline_one_tickets_repeat_same_seat() -> None:
    tickets = [
        {
            "ticket_id": f"TK-one-{index}",
            "abandoned_count": 1,
            "last_abandoned_by": "AI-repeat",
            "last_abandoned_at": ago(10 + index),
        }
        for index in range(3)
    ]
    findings, _, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", tickets, {}, NOW
    )
    assert uncertainty == []
    assert len(history) == 3
    repeats = [item for item in findings if item["kind"] == "repeat-abandoner"]
    assert len(repeats) == 1
    assert repeats[0]["holder_agent_id"] == "AI-repeat"
    assert repeats[0]["dropped_claims"] == 3


def test_uncertainty_aggregates_across_tickets_and_persists() -> None:
    tickets = [
        {
            "ticket_id": "TK-unknown-two",
            "abandoned_count": 3,
            "last_abandoned_by": "AI-one-proven",
            "last_abandoned_at": ago(10),
        },
        {
            "ticket_id": "TK-another-exact",
            "abandoned_count": 1,
            "last_abandoned_by": "AI-other",
            "last_abandoned_at": ago(20),
        },
    ]
    findings, counters, history, uncertainty = coordinator.update_drop_evidence(
        "board-a",
        tickets,
        {"drop_counters": {"TK-unknown-two": 0, "TK-another-exact": 0}},
        NOW,
    )
    incomplete = [
        item for item in findings if item["kind"] == "repeat-abandoner-history-incomplete"
    ]
    assert len(incomplete) == 1
    assert incomplete[0]["unattributed_dropped_claims"] == 2
    assert incomplete[0]["possible_dropped_claims"] == 3

    findings2, _, _, uncertainty2 = coordinator.update_drop_evidence(
        "board-a",
        tickets,
        {
            "drop_counters": counters,
            "drop_history": history,
            "drop_uncertainty": uncertainty,
        },
        NOW + timedelta(seconds=60),
    )
    assert [item["kind"] for item in findings2] == [
        "repeat-abandoner-history-incomplete"
    ]
    assert uncertainty2 == uncertainty


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def test_ancestor_check_logic_with_fake_git_dir(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    tracked.write_text("two\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "second")
    second = _git(tmp_path, "rev-parse", "HEAD")

    assert coordinator.commit_is_ancestor(tmp_path, first, "HEAD") is True
    assert coordinator.commit_is_ancestor(tmp_path, second, first) is False
    assert coordinator.commit_is_ancestor(tmp_path, "not-a-hash", "HEAD") is None


def test_commit_parser_is_conservative_and_ignores_ticket_ids() -> None:
    assert coordinator.extract_commit_hash(
        {"submission_history": [{"notes": "ticket_id: TK-08e894f1a596"}]}
    ) is None
    assert coordinator.extract_commit_hash(
        {"submission_history": [{"notes": "commit_hash: abcdef123456"}]}
    ) == "abcdef123456"


def test_no_merge_needed_review_label_skips_integration_check(tmp_path: Path) -> None:
    project = coordinator.Project("sample", "board-a", tmp_path)
    ticket = {
        "ticket_id": "TK-skip",
        "status": "closed",
        "target_url": "sample/path",
        "submission_history": [{"commit_hash": "abcdef123456"}],
        "review_history": [{"review_label": "no-merge-needed"}],
    }
    assert coordinator.integration_findings(project, [ticket]) == []


def _non_ancestor_fixture(tmp_path: Path) -> tuple[Any, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "first")
    integration_ref = _git(tmp_path, "rev-parse", "HEAD")
    tracked.write_text("two\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "second")
    submitted = _git(tmp_path, "rev-parse", "HEAD")
    project = coordinator.Project("sample", "board-a", tmp_path, integration_ref)
    return project, submitted


def _closed_ticket(commit: str, closed_at: datetime) -> dict[str, object]:
    return {
        "ticket_id": "TK-integration",
        "status": "closed",
        "target_url": "sample/path",
        "reviewed_at": closed_at.isoformat(),
        "submission_history": [{"commit_hash": commit}],
    }


def test_pre_watermark_ticket_is_suppressed_and_counted(tmp_path: Path) -> None:
    project, submitted = _non_ancestor_fixture(tmp_path)
    ticket = _closed_ticket(submitted, NOW - timedelta(seconds=1))

    findings, suppressed = coordinator.evaluate_integration_watch(
        project, [ticket], NOW
    )
    state = coordinator.bound_findings_state(
        findings,
        NOW,
        integration_watch_since=NOW,
        suppressed_pre_watermark=suppressed,
    )

    assert findings == []
    assert state["suppressed_pre_watermark"] == 1
    assert state["integration_watch_since"] == NOW.isoformat()


def test_unknown_commit_object_is_informational(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "first")
    project = coordinator.Project("sample", "board-a", tmp_path, "HEAD")
    ticket = _closed_ticket("a" * 40, NOW + timedelta(seconds=1))

    findings, suppressed = coordinator.evaluate_integration_watch(
        project, [ticket], NOW
    )

    assert suppressed == 0
    assert [(item["kind"], item["level"]) for item in findings] == [
        ("unverifiable-commit", "info")
    ]


def test_post_watermark_non_ancestor_remains_warning(tmp_path: Path) -> None:
    project, submitted = _non_ancestor_fixture(tmp_path)
    ticket = _closed_ticket(submitted, NOW + timedelta(seconds=1))

    findings, suppressed = coordinator.evaluate_integration_watch(
        project, [ticket], NOW
    )

    assert suppressed == 0
    assert [(item["kind"], item["level"]) for item in findings] == [
        ("closed-but-unmerged", "warn")
    ]


def test_ticket_closed_exactly_at_watermark_is_not_suppressed(tmp_path: Path) -> None:
    project, submitted = _non_ancestor_fixture(tmp_path)
    ticket = _closed_ticket(submitted, NOW)

    findings, suppressed = coordinator.evaluate_integration_watch(
        project, [ticket], NOW
    )

    assert suppressed == 0
    assert findings[0]["kind"] == "closed-but-unmerged"


def test_integration_watch_cli_validates_iso_timestamp() -> None:
    args = coordinator.parse_args(
        [
            "--token-path",
            "/private/token",
            "--integration-watch-since",
            "2026-08-26T00:00:00Z",
        ]
    )
    assert coordinator.parse_time(args.integration_watch_since) == datetime(
        2026, 8, 26, tzinfo=timezone.utc
    )
    with pytest.raises(SystemExit):
        coordinator.parse_args(
            [
                "--token-path",
                "/private/token",
                "--integration-watch-since",
                "not-a-timestamp",
            ]
        )


def test_privacy_gate_never_leaks_terms_into_findings(tmp_path: Path) -> None:
    secret_term = "private-marker-value"
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "public.txt").write_text(f"prefix {secret_term} suffix\n", encoding="utf-8")
    _git(tmp_path, "add", "public.txt")
    _git(tmp_path, "commit", "-qm", "content")
    project = coordinator.Project("sample", "board-a", tmp_path, "HEAD", True)

    findings, watermark = coordinator.privacy_findings(project, [secret_term], None)

    rendered = json.dumps(findings)
    assert watermark == _git(tmp_path, "rev-parse", "HEAD")
    assert findings[0]["kind"] == "privacy-leak-suspect"
    assert findings[0]["matched_file_count"] == 1
    assert secret_term not in rendered


def test_privacy_terms_file_must_be_outside_registered_work_dirs(tmp_path: Path) -> None:
    terms_path = tmp_path / "terms.txt"
    terms_path.write_text("sensitive-value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside registered work directories"):
        coordinator.load_privacy_terms(str(terms_path), [tmp_path])


def test_findings_are_bounded_with_explicit_truncation() -> None:
    findings = [
        {"kind": "sample", "level": "warn", "board_id": "board-a", "message": "x" * 2_000, "index": index}
        for index in range(80)
    ]
    state = coordinator.bound_findings_state(findings, NOW)
    rendered = json.dumps(state, sort_keys=True, separators=(",", ":"))

    assert len(state["findings"]) <= 50
    assert state["truncation"]["findings"] == 80 - len(state["findings"])
    assert len(rendered) <= 5_000
    assert all(len(json.dumps(item, sort_keys=True, separators=(",", ":"))) <= 500 for item in state["findings"])
    assert all(0 < len(item["evidence"]) <= 300 for item in state["findings"])
    assert all(item["next_action"] for item in state["findings"])


def test_critical_privacy_finding_survives_warning_bound_and_digest() -> None:
    findings = [
        {
            "kind": "warning",
            "level": "warn",
            "board_id": "board-a",
            "message": f"warning {index}",
        }
        for index in range(50)
    ]
    findings.append(
        {
            "kind": "privacy-leak-suspect",
            "level": "critical",
            "board_id": "board-a",
            "message": "A public integration commit requires privacy review.",
            "commit_hash": "abcdef123456",
            "matched_file_count": 1,
        }
    )
    state = coordinator.bound_findings_state(findings, NOW)

    assert state["findings"][0]["kind"] == "privacy-leak-suspect"
    assert state["truncation"]["findings"] >= 1
    digest = coordinator.format_digest("daily", NOW, {"board-a": state})
    assert "critical=1" in digest
    assert "privacy-leak-suspect=1" in digest


def test_digest_formatting_reports_bounds_and_retention() -> None:
    content = coordinator.format_digest(
        "daily",
        NOW,
        {
            "board-a": {
                "findings": [{"kind": "starved", "level": "warn"}],
                "truncation": {"findings": 2},
            }
        },
    )
    assert "board-a: 1 finding(s), 2 omitted" in content
    assert "warn=1" in content
    assert "starved=1" in content
    assert "30 days" in content


def test_fairness_is_critical_then_oldest_across_boards() -> None:
    project_a = coordinator.Project("a", "board-a", Path("/tmp"))
    project_b = coordinator.Project("b", "board-b", Path("/tmp"))
    snapshots = {
        "board-a": {"board": {}, "agents": [], "tickets": [{"ticket_id": "TK-normal", "status": "open", "priority": "medium", "created_at": ago(7_200)}]},
        "board-b": {"board": {}, "agents": [], "tickets": [{"ticket_id": "TK-critical", "status": "open", "priority": "critical", "created_at": ago(1_200)}]},
    }
    states = coordinator.analyze_cycle([project_a, project_b], snapshots, {}, (), NOW)
    ranks = {
        item["ticket_id"]: item["fleet_fairness_rank"]
        for state in states.values()
        for item in state["findings"]
        if item["kind"] == "starved"
    }
    assert ranks == {"TK-critical": 1, "TK-normal": 2}


@pytest.mark.parametrize(
    "degraded_snapshot",
    [
        {
            "board": {"board_id": "board-a"},
            "agents": [],
            "tickets": [],
            "snapshot_error_class": "TimeoutError",
            "truncated": True,
            "omitted_counts": {"agents": 1, "tickets": 1},
        },
        {
            "board": {"board_id": "board-a"},
            "agents": [],
            "tickets": [],
            "truncated": True,
            "omitted_counts": {"tickets": 2},
        },
    ],
)
def test_board_degraded_after_three_polls_and_resets_on_success(
    degraded_snapshot: dict[str, Any], tmp_path: Path
) -> None:
    project = coordinator.Project("sample", "board-a", tmp_path)
    streaks: dict[str, int] = {}
    state: dict[str, Any] = {}

    for expected_streak in (1, 2, 3):
        states = coordinator.analyze_cycle(
            [project],
            {"board-a": degraded_snapshot},
            {"board-a": state},
            (),
            NOW,
            degraded_streaks=streaks,
        )
        state = states["board-a"]
        assert state["board_health"]["consecutive_degraded_polls"] == expected_streak
        degraded = [
            item for item in state["findings"] if item["kind"] == "board-degraded"
        ]
        assert bool(degraded) is (expected_streak == 3)

    finding = next(
        item for item in state["findings"] if item["kind"] == "board-degraded"
    )
    assert finding["level"] == "critical"
    assert "observed_consecutive_polls=3" in finding["evidence"]
    assert "threshold_polls=3" in finding["evidence"]
    assert "TimeoutError" not in finding.get("message", "")
    if "snapshot_error_class" in degraded_snapshot:
        assert "error_class=TimeoutError" in finding["evidence"]

    healthy = {"board": {"board_id": "board-a"}, "agents": [], "tickets": []}
    reset = coordinator.analyze_cycle(
        [project],
        {"board-a": healthy},
        {"board-a": state},
        (),
        NOW,
        degraded_streaks=streaks,
    )["board-a"]
    assert reset["board_health"] == {
        "status": "healthy",
        "consecutive_degraded_polls": 0,
        "reason": None,
        "error_class": None,
    }
    assert all(item["kind"] != "board-degraded" for item in reset["findings"])


def test_read_cycle_records_only_snapshot_error_class(tmp_path: Path) -> None:
    sensitive_detail = "transport failed with sensitive-detail-value"

    class Reader:
        async def call(self, name: str, board_id: str, **arguments: Any) -> dict:
            if name == "board_state_get" and arguments.get("key") == "project_registry":
                return {
                    "state": {
                        "value": json.dumps(
                            {
                                "schema_version": 1,
                                "projects": {
                                    "sample": {
                                        "board_id": "board-a",
                                        "work_dir": str(tmp_path),
                                        "status": "active",
                                    }
                                },
                            }
                        )
                    }
                }
            if name == "board_snapshot":
                raise RuntimeError(sensitive_detail)
            if name == "board_state_get":
                raise RuntimeError("optional state unavailable")
            raise AssertionError(f"unexpected call: {name}")

    _projects, snapshots, previous = asyncio.run(
        coordinator.read_cycle(Reader(), "pursers")
    )

    assert snapshots["board-a"]["snapshot_error_class"] == "RuntimeError"
    assert sensitive_detail not in json.dumps(snapshots)
    assert previous == {"board-a": {}}


def test_write_reports_isolates_failed_board_and_mirrors_degraded_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pursers_client

    sensitive_detail = "state write failed with sensitive-detail-value"
    attempts: list[str] = []
    published: dict[str, dict[str, Any]] = {}

    class FakeBoardClient:
        def __init__(
            self, _url: str, _token: str, board_id: str, *, agent_name: str
        ) -> None:
            self.board_id = board_id

        async def __aenter__(self) -> "FakeBoardClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def board_state_update(self, key: str, value: str) -> dict[str, bool]:
            assert key == coordinator.STATE_KEY
            attempts.append(self.board_id)
            if self.board_id == "broken-board":
                raise RuntimeError(sensitive_detail)
            published[self.board_id] = json.loads(value)
            return {"ok": True}

        async def memory_write(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("digest markers should suppress memory writes")

    monkeypatch.setattr(pursers_client, "BoardClient", FakeBoardClient)
    degraded = coordinator._finding(
        "board-degraded",
        "critical",
        "broken-board",
        "The board snapshot has been unavailable or incomplete for 3 polls.",
        error_class="TimeoutError",
        observed_consecutive_polls=3,
        threshold_polls=3,
    )
    states = {
        "broken-board": coordinator.bound_findings_state(
            [degraded],
            NOW,
            board_health={
                "status": "degraded",
                "consecutive_degraded_polls": 3,
                "reason": "snapshot-error",
                "error_class": "TimeoutError",
            },
        ),
        "healthy-board": coordinator.bound_findings_state([], NOW),
        "home-board": coordinator.bound_findings_state([], NOW),
    }
    states["home-board"]["last_daily_digest"] = NOW.date().isoformat()
    states["home-board"]["last_weekly_digest"] = (
        f"{NOW.isocalendar().year}-W{NOW.isocalendar().week:02d}"
    )
    previous = {
        "home-board": {
            "last_daily_digest": NOW.date().isoformat(),
            "last_weekly_digest": (
                f"{NOW.isocalendar().year}-W{NOW.isocalendar().week:02d}"
            ),
        }
    }

    asyncio.run(
        coordinator.write_reports(
            "https://board.invalid/mcp",
            "not-a-real-token",
            "home-board",
            "coordinator-test",
            states,
            previous,
            NOW,
        )
    )

    assert attempts == ["broken-board", "healthy-board", "home-board"]
    assert set(published) == {"healthy-board", "home-board"}
    home_payload = published["home-board"]
    mirrored = next(
        item
        for item in home_payload["findings"]
        if item["kind"] == "board-degraded"
        and item["board_id"] == "broken-board"
    )
    assert "error_class=TimeoutError" in mirrored["evidence"]
    assert "observed_consecutive_polls=3" in mirrored["evidence"]
    assert sensitive_detail not in json.dumps(home_payload)
    assert len(json.dumps(home_payload, separators=(",", ":"))) <= coordinator.MAX_STATE_CHARS


def action(kind: str, index: int = 0) -> coordinator.Action:
    return coordinator.Action(
        kind=kind,
        board_id="board-a",
        ticket_id=f"TK-{index}",
        target_agent_id="AI-target",
        target_agent_name="worker-target",
        stage=1 if kind == "nudge" else 2,
        threshold_seconds=1_800,
        threshold_window=1 if kind == "nudge" else 2,
        op_key=f"coord-op-{kind}-{index}",
        reason="deterministic test decision",
    )


def test_shadow_mode_emits_would_findings_and_makes_zero_mutation_calls() -> None:
    calls: list[coordinator.Action] = []

    async def fake_mutate(item: coordinator.Action) -> dict[str, bool]:
        calls.append(item)
        return {"ok": True}

    runtime = coordinator.RuntimeState.for_mode("shadow")
    findings, histories = asyncio.run(
        coordinator.execute_actions(
            [action("nudge"), action("assign", 1)],
            fake_mutate,
            runtime,
            NOW,
            {},
        )
    )

    assert calls == []
    assert [item["kind"] for item in findings] == ["would_nudge", "would_assign"]
    assert histories == {}


def test_active_assignment_precondition_race_is_reported_without_retry() -> None:
    calls: list[coordinator.Action] = []

    async def fake_mutate(item: coordinator.Action) -> None:
        calls.append(item)
        raise RuntimeError("assignment state precondition failed")

    runtime = coordinator.RuntimeState.for_mode("active")
    findings, histories = asyncio.run(
        coordinator.execute_actions(
            [action("assign")], fake_mutate, runtime, NOW, {}
        )
    )

    assert len(calls) == 1
    assert findings[0]["kind"] == "mutation_failed"
    assert "state precondition failed" in findings[0]["error"]
    assert histories == {}


def test_action_idempotency_key_is_stable_across_restart() -> None:
    snapshot = {
        "board-a": {
            "board": {},
            "agents": [
                {
                    "agent_id": "AI-free",
                    "agent_name": "worker",
                    "last_activity_at": ago(1),
                    "status": "active",
                    "membership_role": "member",
                }
            ],
            "tickets": [
                {
                    "ticket_id": "TK-old",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(3_600),
                }
            ],
        }
    }
    states = {"board-a": {"drop_history": []}}

    first = coordinator.plan_actions(snapshot, states, {}, NOW)
    second = coordinator.plan_actions(snapshot, states, {}, NOW)

    assert len(first) == len(second) == 1
    assert first[0].op_key == second[0].op_key
    assert first[0].threshold_window == 2


def test_rate_limits_assignment_and_nudges_across_restart() -> None:
    agents = [
        {
            "agent_id": "AI-free",
            "agent_name": "worker",
            "last_activity_at": ago(1),
            "status": "active",
            "membership_role": "member",
        }
    ]
    previous = {
        "board-a": {
            "action_history": [
                {
                    "kind": "nudge",
                    "target_agent_id": "AI-free",
                    "performed_at": ago(100 + index),
                }
                for index in range(3)
            ]
        }
    }
    stage_one = {
        "board-a": {
            "board": {},
            "agents": agents,
            "tickets": [
                {
                    "ticket_id": "TK-stage-one",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(1_800),
                }
            ],
        }
    }
    assert coordinator.plan_actions(
        stage_one, {"board-a": {"drop_history": []}}, previous, NOW
    ) == []

    previous["board-a"]["action_history"] = [
        {"kind": "assign", "performed_at": ago(599)}
    ]
    stage_two = {
        "board-a": {
            "board": {},
            "agents": agents,
            "tickets": [
                {
                    "ticket_id": "TK-stage-two",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(3_600),
                }
            ],
        }
    }
    assert coordinator.plan_actions(
        stage_two, {"board-a": {"drop_history": []}}, previous, NOW
    ) == []
    previous["board-a"]["action_history"][0]["performed_at"] = ago(600)
    assert [item.kind for item in coordinator.plan_actions(
        stage_two, {"board-a": {"drop_history": []}}, previous, NOW
    )] == ["assign"]


def test_three_mutation_failures_open_circuit_and_remaining_actions_are_shadowed() -> None:
    calls: list[coordinator.Action] = []

    async def fail(item: coordinator.Action) -> None:
        calls.append(item)
        raise RuntimeError("write failed")

    runtime = coordinator.RuntimeState.for_mode("active")
    findings, _ = asyncio.run(
        coordinator.execute_actions(
            [action("nudge", index) for index in range(4)],
            fail,
            runtime,
            NOW,
            {},
        )
    )

    assert len(calls) == 3
    assert runtime.effective_mode == "shadow"
    assert sum(item["kind"] == "mutation_failed" for item in findings) == 3
    assert any(item["kind"] == "coordinator_circuit_open" for item in findings)
    assert findings[-1]["kind"] == "would_nudge"


def test_repeat_abandoner_is_deprioritized_using_live_pool_eligibility() -> None:
    snapshot = {
        "board-a": {
            "board": {},
            "agents": [
                {
                    "agent_id": "AI-repeat",
                    "agent_name": "worker-repeat",
                    "last_activity_at": ago(1),
                    "status": "active",
                    "membership_role": "member",
                },
                {
                    "agent_id": "AI-clean",
                    "agent_name": "worker-clean",
                    "last_activity_at": ago(2),
                    "status": "active",
                    "membership_role": "member",
                },
            ],
            "tickets": [
                {
                    "ticket_id": "TK-old",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(3_600),
                }
            ],
        }
    }
    states = {
        "board-a": {
            "drop_history": [
                {
                    "holder_agent_id": "AI-repeat",
                    "count": 3,
                    "observed_at": ago(10),
                }
            ]
        }
    }

    planned = coordinator.plan_actions(snapshot, states, {}, NOW)

    assert len(planned) == 1
    assert planned[0].target_agent_id == "AI-clean"


def test_restart_kill_switch_defaults_to_shadow(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("opaque", encoding="utf-8")

    default_args = coordinator.parse_args(["--token-path", str(token), "--once"])
    active_args = coordinator.parse_args(
        ["--token-path", str(token), "--once", "--mode", "active"]
    )

    assert default_args.mode == "shadow"
    assert active_args.mode == "active"
    assert default_args.review_backlog_seconds == 1_800
    tuned = coordinator.parse_args(
        [
            "--token-path",
            str(token),
            "--review-backlog-seconds",
            "90",
        ]
    )
    assert tuned.review_backlog_seconds == 90
    with pytest.raises(SystemExit):
        coordinator.parse_args(
            [
                "--token-path",
                str(token),
                "--review-backlog-seconds",
                "0",
            ]
        )


def test_coordination_uses_complete_active_list_but_fails_closed_on_missing_agents() -> None:
    active_ticket = {
        "ticket_id": "TK-complete-active",
        "status": "open",
        "priority": "medium",
        "created_at": ago(3_600),
    }
    agent = {
        "agent_id": "AI-live",
        "agent_name": "worker-live",
        "last_activity_at": ago(1),
        "status": "active",
        "membership_role": "member",
    }
    snapshot = {
        "board-a": {
            "agents": [agent],
            "tickets": [],
            "coordination_tickets": [active_ticket],
            "coordination_tickets_complete": True,
            "truncated": True,
            "omitted_counts": {"tickets": 20, "agents": 0},
        }
    }
    states = {"board-a": {"drop_history": []}}

    assert [item.kind for item in coordinator.plan_actions(
        snapshot, states, {}, NOW
    )] == ["assign"]
    snapshot["board-a"]["omitted_counts"]["agents"] = 1
    assert coordinator.plan_actions(snapshot, states, {}, NOW) == []


def test_truncated_rate_history_fails_closed_until_safety_window_expires() -> None:
    snapshot = {
        "board-a": {
            "agents": [
                {
                    "agent_id": "AI-live",
                    "agent_name": "worker-live",
                    "last_activity_at": ago(1),
                    "status": "active",
                    "membership_role": "member",
                }
            ],
            "tickets": [
                {
                    "ticket_id": "TK-rate-history",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(3_600),
                }
            ],
        }
    }
    future = (NOW + timedelta(seconds=1)).isoformat()
    previous = {"board-a": {"action_history_incomplete_until": future}}

    assert coordinator.plan_actions(
        snapshot, {"board-a": {"drop_history": []}}, previous, NOW
    ) == []
    assert [item.kind for item in coordinator.plan_actions(
        snapshot,
        {"board-a": {"drop_history": []}},
        previous,
        NOW + timedelta(seconds=2),
    )] == ["assign"]
