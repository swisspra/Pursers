from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

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


def test_force_assignment_respects_advertised_max_tier() -> None:
    snapshot = {
        "board-a": {
            "agents": [
                {
                    "agent_id": "AI-light",
                    "agent_name": "worker-light",
                    "last_activity_at": ago(20),
                    "status": "active",
                    "membership_role": "member",
                    "task_focus": "worker-runtime max_tier=light",
                },
                {
                    "agent_id": "AI-heavy",
                    "agent_name": "worker-heavy",
                    "last_activity_at": ago(10),
                    "status": "active",
                    "membership_role": "member",
                    "task_focus": "worker-runtime max_tier=heavy",
                },
            ],
            "tickets": [
                {
                    "ticket_id": "TK-heavy",
                    "status": "open",
                    "priority": "medium",
                    "created_at": ago(3_601),
                    "tags": ["tier:heavy"],
                }
            ],
        }
    }

    actions = coordinator.plan_actions(
        snapshot, {"board-a": {"drop_history": []}}, {}, NOW
    )

    assert len(actions) == 1
    assert actions[0].kind == "assign"
    assert actions[0].target_agent_id == "AI-heavy"

    snapshot["board-a"]["agents"] = snapshot["board-a"]["agents"][:1]
    assert coordinator.plan_actions(
        snapshot, {"board-a": {"drop_history": []}}, {}, NOW
    ) == []


def test_absent_ticket_tier_defaults_standard_for_coordinator() -> None:
    light = {"task_focus": "worker-runtime max_tier=light"}
    standard = {"task_focus": "worker-runtime max_tier=standard"}
    ticket = {"tags": []}

    assert coordinator.ticket_tier(ticket) == "standard"
    assert coordinator.tier_allows(light, ticket) is False
    assert coordinator.tier_allows(standard, ticket) is True


@pytest.mark.parametrize(
    "kind",
    [
        "starved",
        "claim-health",
        "snapshot-truncated",
        "board-large",
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


def test_board_degraded_after_three_polls_and_resets_on_success(
    tmp_path: Path,
) -> None:
    degraded_snapshot = {
        "board": {"board_id": "board-a"},
        "agents": [],
        "tickets": [],
        "snapshot_error_class": "TimeoutError",
        "truncated": True,
        "omitted_counts": {"agents": 1, "tickets": 1},
    }
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


def test_truncation_only_is_board_large_info_and_refreshes_daily(
    tmp_path: Path,
) -> None:
    project = coordinator.Project("sample", "board-a", tmp_path)
    snapshot = {
        "board": {"board_id": "board-a"},
        "agents": [],
        "tickets": [],
        "truncated": True,
        "returned_counts": {"tickets": 44, "agents": 8},
        "omitted_counts": {"tickets": 74, "agents": 0},
        "total_counts": {"tickets": 118, "agents": 8},
    }
    streaks: dict[str, int] = {}
    first = coordinator.analyze_cycle(
        [project], {"board-a": snapshot}, {}, (), NOW, degraded_streaks=streaks
    )["board-a"]
    first_large = [item for item in first["findings"] if item["kind"] == "board-large"]

    assert len(first_large) == 1
    assert first_large[0]["level"] == "info"
    assert first_large[0]["returned_counts"] == {"tickets": 44}
    assert first_large[0]["total_counts"] == {"tickets": 118}
    assert "returned_counts={\"tickets\":44}" in first_large[0]["evidence"]
    assert "total_counts={\"tickets\":118}" in first_large[0]["evidence"]
    assert "journal compaction" in first_large[0]["next_action"]
    assert all(item["kind"] != "board-degraded" for item in first["findings"])
    assert first["board_health"]["status"] == "healthy"
    assert first["board_health"]["consecutive_degraded_polls"] == 0

    same_day = coordinator.analyze_cycle(
        [project],
        {"board-a": snapshot},
        {"board-a": first},
        (),
        NOW + timedelta(hours=23),
        degraded_streaks=streaks,
    )["board-a"]
    same_day_large = next(
        item for item in same_day["findings"] if item["kind"] == "board-large"
    )
    assert same_day_large["refreshed_at"] == first_large[0]["refreshed_at"]

    next_day = coordinator.analyze_cycle(
        [project],
        {"board-a": snapshot},
        {"board-a": same_day},
        (),
        NOW + timedelta(days=1, seconds=1),
        degraded_streaks=streaks,
    )["board-a"]
    next_day_large = next(
        item for item in next_day["findings"] if item["kind"] == "board-large"
    )
    assert next_day_large["refreshed_at"] != first_large[0]["refreshed_at"]


def test_mixed_truncation_and_call_failures_do_not_share_streak(
    tmp_path: Path,
) -> None:
    project = coordinator.Project("sample", "board-a", tmp_path)
    truncated = {
        "board": {"board_id": "board-a"},
        "agents": [],
        "tickets": [],
        "truncated": True,
        "returned_counts": {"tickets": 44},
        "omitted_counts": {"tickets": 74},
        "total_counts": {"tickets": 118},
    }
    failed = {
        "board": {"board_id": "board-a"},
        "agents": [],
        "tickets": [],
        "state_error_classes": {coordinator.STATE_KEY: "TimeoutError"},
    }
    streaks: dict[str, int] = {}
    prior: dict[str, Any] = {}
    for snapshot, expected in ((truncated, 0), (failed, 1), (truncated, 0)):
        prior = coordinator.analyze_cycle(
            [project],
            {"board-a": snapshot},
            {"board-a": prior},
            (),
            NOW,
            degraded_streaks=streaks,
        )["board-a"]
        assert prior["board_health"]["consecutive_degraded_polls"] == expected
        assert all(item["kind"] != "board-degraded" for item in prior["findings"])


def test_state_call_failure_streak_becomes_degraded(tmp_path: Path) -> None:
    project = coordinator.Project("sample", "board-a", tmp_path)
    failed = {
        "board": {"board_id": "board-a"},
        "agents": [],
        "tickets": [],
        "state_error_classes": {coordinator.STATE_KEY: "TimeoutError"},
    }
    streaks: dict[str, int] = {}
    prior: dict[str, Any] = {}
    for expected_streak in (1, 2, 3):
        prior = coordinator.analyze_cycle(
            [project],
            {"board-a": failed},
            {"board-a": prior},
            (),
            NOW,
            degraded_streaks=streaks,
        )["board-a"]
        assert prior["board_health"]["consecutive_degraded_polls"] == expected_streak

    finding = next(
        item for item in prior["findings"] if item["kind"] == "board-degraded"
    )
    assert finding["degradation_reason"] == "state-failed"
    assert finding["error_class"] == "TimeoutError"


def test_board_level_finding_dedupe_keeps_latest() -> None:
    old = coordinator._finding(
        "board-large", "info", "board-a", "old", refreshed_at=ago(60)
    )
    latest = coordinator._finding(
        "board-large", "info", "board-a", "latest", refreshed_at=ago(30)
    )
    distinct_ticket = [
        coordinator._finding(
            "starved", "warn", "board-a", "one", ticket_id="TK-1"
        ),
        coordinator._finding(
            "starved", "warn", "board-a", "two", ticket_id="TK-2"
        ),
    ]

    state = coordinator.bound_findings_state([old, *distinct_ticket, latest], NOW)

    board_large = [item for item in state["findings"] if item["kind"] == "board-large"]
    assert len(board_large) == 1
    assert board_large[0]["message"] == "latest"
    assert sum(item["kind"] == "starved" for item in state["findings"]) == 2


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
    assert snapshots["board-a"]["state_error_classes"] == {
        coordinator.STATE_KEY: "RuntimeError",
    }
    assert sensitive_detail not in json.dumps(snapshots)
    assert previous == {"board-a": {}}


@pytest.mark.parametrize(
    "missing_key", [coordinator.STATE_KEY, coordinator.CONFIG_STATE_KEY]
)
def test_read_cycle_missing_optional_state_stays_healthy_for_three_polls(
    tmp_path: Path, missing_key: str
) -> None:
    class Reader:
        async def call(self, name: str, board_id: str, **arguments: Any) -> dict:
            key = arguments.get("key")
            if name == "board_state_get" and key == "project_registry":
                return {
                    "state": {
                        "value": json.dumps(
                            {
                                "schema_version": 1,
                                "projects": {
                                    "sample": {
                                        "board_id": "pursers",
                                        "work_dir": str(tmp_path),
                                        "status": "active",
                                    }
                                },
                            }
                        )
                    }
                }
            if name == "board_snapshot":
                return {
                    "board": {"board_id": board_id},
                    "agents": [],
                    "tickets": [],
                    "truncated": False,
                }
            if name == "ticket_list":
                return {"count": 0, "total_matching": 0, "tickets": []}
            if name == "board_state_get" and key == missing_key:
                raise ValueError("state key not found")
            if name == "board_state_get" and key == coordinator.INTAKE_STATE_KEY:
                raise ValueError("state key not found")
            if name == "board_state_get":
                return {"state": {"value": "{}"}}
            raise AssertionError(f"unexpected call: {name}")

    streaks: dict[str, int] = {}
    for _poll in range(3):
        projects, snapshots, previous = asyncio.run(
            coordinator.read_cycle(Reader(), "pursers")
        )
        assert "state_error_classes" not in snapshots["pursers"]
        state = coordinator.analyze_cycle(
            projects,
            snapshots,
            previous,
            (),
            NOW,
            degraded_streaks=streaks,
        )["pursers"]
        assert state["board_health"]["status"] == "healthy"
        assert state["board_health"]["consecutive_degraded_polls"] == 0
        assert all(
            item["kind"] != "board-degraded" for item in state["findings"]
        )


def test_read_cycle_real_optional_state_failure_degrades_after_three_polls(
    tmp_path: Path,
) -> None:
    class Reader:
        async def call(self, name: str, board_id: str, **arguments: Any) -> dict:
            key = arguments.get("key")
            if name == "board_state_get" and key == "project_registry":
                return {
                    "state": {
                        "value": json.dumps(
                            {
                                "schema_version": 1,
                                "projects": {
                                    "sample": {
                                        "board_id": "pursers",
                                        "work_dir": str(tmp_path),
                                        "status": "active",
                                    }
                                },
                            }
                        )
                    }
                }
            if name == "board_snapshot":
                return {
                    "board": {"board_id": board_id},
                    "agents": [],
                    "tickets": [],
                    "truncated": False,
                }
            if name == "ticket_list":
                return {"count": 0, "total_matching": 0, "tickets": []}
            if name == "board_state_get" and key == coordinator.STATE_KEY:
                raise TimeoutError("state service timed out")
            if name == "board_state_get" and key == coordinator.INTAKE_STATE_KEY:
                raise ValueError("state key not found")
            if name == "board_state_get":
                return {"state": {"value": "{}"}}
            raise AssertionError(f"unexpected call: {name}")

    streaks: dict[str, int] = {}
    state: dict[str, Any] = {}
    for expected_streak in (1, 2, 3):
        projects, snapshots, previous = asyncio.run(
            coordinator.read_cycle(Reader(), "pursers")
        )
        assert snapshots["pursers"]["state_error_classes"] == {
            coordinator.STATE_KEY: "TimeoutError"
        }
        state = coordinator.analyze_cycle(
            projects,
            snapshots,
            previous,
            (),
            NOW,
            degraded_streaks=streaks,
        )["pursers"]
        assert (
            state["board_health"]["consecutive_degraded_polls"]
            == expected_streak
        )

    finding = next(
        item for item in state["findings"] if item["kind"] == "board-degraded"
    )
    assert finding["degradation_reason"] == "state-failed"
    assert finding["error_class"] == "TimeoutError"


def test_write_reports_isolates_failed_board_and_mirrors_degraded_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pursers_client

    sensitive_detail = "state write failed with sensitive-detail-value"
    attempts: list[str] = []
    published: dict[str, dict[str, Any]] = {}

    class FakeBoardClient:
        agent_name = "coordinator-test"

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


@pytest.mark.parametrize("category", coordinator.INTAKE_CATEGORIES)
@pytest.mark.parametrize("domain", ["personal", "work"])
def test_intake_approval_matrix_every_category_by_domain(
    category: str, domain: str
) -> None:
    decision, _rule = coordinator.intake_matrix_decision(
        category, domain, has_clear_reproduction=(category == "bug")
    )
    expected = (
        "auto"
        if domain == "personal"
        and category in {"docs", "tests", "audit-analysis", "bug"}
        else "ask"
    )
    assert decision == expected


def test_personal_bug_requires_clear_reproduction() -> None:
    assert coordinator.intake_matrix_decision("bug", "personal", False)[0] == "ask"
    assert coordinator.intake_matrix_decision("bug", "personal", True)[0] == "auto"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Update the README guide", "docs"),
        ("Add pytest coverage", "tests"),
        ("Run a read-only audit", "audit-analysis"),
        ("Fix bug; steps to reproduce are listed", "bug"),
        ("Implement the new parser", "production-code"),
        ("Publish the next release", "release-ci"),
        ("Change the reviewer role", "membership-roles"),
        ("Update project_registry routing", "board-registry"),
    ],
)
def test_deterministic_intake_classifier(text: str, category: str) -> None:
    assert coordinator.classify_intake(text)[0] == category


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Change production code and update docs", "production-code"),
        ("Implement a new API endpoint with tests", "production-code"),
        ("Update the parser and its documentation", "production-code"),
        ("Add a new API endpoint with tests", "production-code"),
        ("Rewrite the service and add pytest coverage", "production-code"),
        ("Improve runtime behavior and update the README", "production-code"),
        ("Build the backend and document it", "production-code"),
        ("Extend the handler with unit tests", "production-code"),
        ("Optimize the database and update the guide", "production-code"),
        ("Replace the runtime module and add coverage", "production-code"),
        ("Patch the parser; documentation included", "production-code"),
        ("Rework the backend, tests included", "production-code"),
        ("Harden the service; pytest coverage included", "production-code"),
        ("Instrument the handler; tests included", "production-code"),
        ("Port the frontend; README included", "production-code"),
        ("Secure the endpoint; test coverage included", "production-code"),
        ("Modernize the application; documentation included", "production-code"),
        (
            "Fix bug; steps to reproduce: run failing example; implement a new endpoint too",
            "production-code",
        ),
        (
            "Bug with traceback in parser; refactor the service and update docs",
            "production-code",
        ),
        (
            "Fix regression with failing example and delete the database schema",
            "production-code",
        ),
        (
            "Crash with traceback; add a new feature after fixing it",
            "production-code",
        ),
        (
            "Broken parser, steps to reproduce included; rewrite the backend",
            "production-code",
        ),
        (
            "Fix bug; steps to reproduce: run failing example and implement an endpoint",
            "production-code",
        ),
        ("Bug with traceback in parser and rewrite service", "production-code"),
        ("Fix bug in parser and delete schema; traceback", "production-code"),
        ("Fix bug in docs", "bug"),
    ],
)
def test_intake_classifier_conservative_mixed_intent(
    text: str, category: str
) -> None:
    assert coordinator.classify_intake(text)[0] == category


def test_mixed_production_intakes_are_draft_only_in_dry_run() -> None:
    texts = [
        "Update the parser and its documentation",
        "Add a new API endpoint with tests",
        "Rewrite the service and add pytest coverage",
        "Improve runtime behavior and update the README",
        "Patch the parser; documentation included",
        "Rework the backend, tests included",
        "Harden the service; pytest coverage included",
        "Instrument the handler; tests included",
        "Port the frontend; README included",
        "Secure the endpoint; test coverage included",
        "Modernize the application; documentation included",
        "Fix bug; steps to reproduce: run failing example; implement a new endpoint too",
        "Bug with traceback in parser; refactor the service and update docs",
        "Fix regression with failing example and delete the database schema",
        "Crash with traceback; add a new feature after fixing it",
        "Broken parser, steps to reproduce included; rewrite the backend",
        "Fix bug; steps to reproduce: run failing example and implement an endpoint",
        "Bug with traceback in parser and rewrite service",
        "Fix bug in parser and delete schema; traceback",
    ]
    rows = [_intake_row(f"ask-mixed-{index}", text) for index, text in enumerate(texts)]

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("dry-run must not create")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state(rows),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=True,
            create_ticket=unexpected_create,
        )
    )

    assert {item["kind"] for item in findings} == {"intake-pending"}
    assert {item["category"] for item in findings} == {"production-code"}
    assert updates == {}


def test_pure_auto_intakes_remain_authorized_in_dry_run() -> None:
    rows = [
        _intake_row("ask-pure-docs", "Update the README guide"),
        _intake_row("ask-pure-tests", "Add pytest coverage"),
        _intake_row("ask-pure-audit", "Run a read-only audit"),
        _intake_row(
            "ask-pure-bug-1", "Fix bug; steps to reproduce: run failing example"
        ),
        _intake_row(
            "ask-pure-bug-2", "Fix regression; failing example attached"
        ),
        _intake_row("ask-pure-bug-3", "Crash with traceback in parser"),
    ]

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("dry-run must not create")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state(rows),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=True,
            create_ticket=unexpected_create,
        )
    )

    assert {item["kind"] for item in findings} == {"intake-would-create"}
    assert {item["category"] for item in findings} == {
        "docs",
        "tests",
        "audit-analysis",
        "bug",
    }
    assert updates == {}


def _intake_state(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"state": {"value": json.dumps(rows)}}


def _intake_project(domain: str = "personal") -> coordinator.Project:
    return coordinator.Project(
        "project-a", "board-a", Path("/tmp/project-a"), domain=domain
    )


def _intake_row(ask_id: str, text: str) -> dict[str, str]:
    return {
        "id": ask_id,
        "text": text,
        "requested_by": "operator",
        "board_id": "board-a",
    }


def test_human_approved_intake_bypasses_matrix_with_same_identity() -> None:
    approved = {
        **_intake_row("ask-approved", "Publish the next release"),
        "approved": True,
        "approved_by": "dashboard-seat",
        "approved_at": NOW.isoformat(),
        "approved_title": "Edited approved release",
    }
    unapproved = _intake_row("ask-unapproved", "Publish another release")
    created: list[tuple[str, coordinator.IntakeDraft]] = []

    async def create(board_id: str, draft: coordinator.IntakeDraft) -> str:
        created.append((board_id, draft))
        return draft.ticket_id

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state(
                        [approved, unapproved]
                    ),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=create,
        )
    )

    assert [(board_id, draft.title) for board_id, draft in created] == [
        ("board-a", "Edited approved release")
    ]
    assert created[0][1].op_key.startswith("coord-intake-")
    assert created[0][1].ticket_id.startswith("TK-intake-")
    assert [item["kind"] for item in findings] == [
        "intake-created",
        "intake-pending",
    ]
    assert findings[0]["matrix_rule"] == "human-approved"
    assert findings[1]["matrix_rule"] == "personal-release-ci-always-ask"
    assert updates == {"board-a": frozenset({"ask-approved"})}


def test_approved_intake_calls_ticket_create_with_op_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, arguments))
            return {"ticket": {"ticket_id": arguments["ticket_id"]}}

    fake_module = type(
        "FakePursersClient",
        (),
        {"BoardClient": FakeClient, "BoardClientError": RuntimeError},
    )
    monkeypatch.setitem(sys.modules, "pursers_client", fake_module)
    approved = {
        **_intake_row("ask-e2e", "Publish the next release"),
        "approved": True,
        "approved_by": "dashboard-seat",
        "approved_at": NOW.isoformat(),
    }

    async def create(board_id: str, draft: coordinator.IntakeDraft) -> str:
        return await coordinator.create_intake_ticket(
            "https://board.invalid/mcp",
            "opaque",
            "coordinator-test",
            board_id,
            draft,
        )

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state([approved]),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=create,
        )
    )

    assert [name for name, _arguments in calls] == ["ticket_create"]
    arguments = calls[0][1]
    assert arguments["coordinator_op_key"].startswith("coord-intake-")
    assert arguments["tags"] == [
        "coordinator-intake",
        f"op:{arguments['coordinator_op_key']}",
    ]
    assert findings[0]["kind"] == "intake-created"
    assert updates == {"board-a": frozenset({"ask-e2e"})}


def test_approved_intake_without_scope_stays_with_grant_finding() -> None:
    approved = {
        **_intake_row("ask-approved", "Publish the next release"),
        "approved": True,
        "approved_by": "dashboard-seat",
        "approved_at": NOW.isoformat(),
    }

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("missing board:intake must not attempt creation")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state([approved]),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=unexpected_create,
            intake_authorized=False,
        )
    )

    assert updates == {}
    assert findings[0]["kind"] == "intake-approved-scope-missing"
    assert findings[0]["matrix_rule"] == "approved-missing-board-intake-grant"
    assert "lacks board:intake" in findings[0]["message"]
    assert "Grant board:intake" in findings[0]["next_action"]
    assert "approved ask remains queued" in findings[0]["next_action"]


def test_approved_intake_keeps_hourly_rate_limit() -> None:
    approved = {
        **_intake_row("ask-approved", "Publish the next release"),
        "approved": True,
        "approved_by": "dashboard-seat",
        "approved_at": NOW.isoformat(),
    }
    tickets = [
        {
            "ticket_id": f"TK-{index}",
            "tags": ["coordinator-intake"],
            "created_at": ago(index + 1),
        }
        for index in range(5)
    ]

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("rate-limited approved intake must not create")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": tickets,
                    "coordinator_intake_state": _intake_state([approved]),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=unexpected_create,
        )
    )

    assert updates == {}
    assert findings[0]["kind"] == "intake-approved-deferred"
    assert findings[0]["matrix_rule"] == "hourly-auto-create-limit"
    assert "remains queued" in findings[0]["next_action"]


def _matching_replay_ticket(draft: coordinator.IntakeDraft) -> dict[str, Any]:
    return {
        "ticket_id": draft.ticket_id,
        "title": draft.title,
        "description": draft.description,
        "scope": draft.scope,
        "target_url": draft.target_url,
        "required_fields": list(draft.required_fields),
        "tags": ["coordinator-intake", f"op:{draft.op_key}"],
        "origin": "coordinator-intake",
        "coordinator_op_key": draft.op_key,
        "priority": "medium",
        "server_generated_id": False,
        "assigned_to": None,
        "assigned_to_agent_id": None,
        "assigned_to_kind": None,
    }


def _install_replay_client(
    monkeypatch: pytest.MonkeyPatch, existing: Mapping[str, Any]
) -> None:
    class ReplayError(Exception):
        pass

    class ReplayClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "ReplayClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def _call(self, *_args: Any, **_kwargs: Any) -> None:
            raise ReplayError("ticket already exists")

        async def ticket_get(self, _ticket_id: str) -> dict[str, Any]:
            return {"ticket": dict(existing)}

    fake_module = type(
        "FakePursersClient",
        (),
        {"BoardClient": ReplayClient, "BoardClientError": ReplayError},
    )
    monkeypatch.setitem(sys.modules, "pursers_client", fake_module)


def test_existing_intake_ticket_exact_replay_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = coordinator.deterministic_intake_draft(
        coordinator.IntakeAsk(
            "ask-replay", "Update the README guide", "operator", "board-a"
        ),
        _intake_project(),
    )
    _install_replay_client(monkeypatch, _matching_replay_ticket(draft))

    ticket_id = asyncio.run(
        coordinator.create_intake_ticket(
            "https://board.invalid/mcp",
            "opaque",
            "coordinator-test",
            "board-a",
            draft,
        )
    )

    assert ticket_id == draft.ticket_id


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("ticket_id", "TK-collision"),
        ("title", "Wrong title"),
        ("description", "Wrong description"),
        ("scope", "read-only"),
        ("target_url", "wrong/target"),
        ("required_fields", ["wrong"]),
        ("tags", ["coordinator-intake"]),
        ("origin", "ordinary-ticket"),
        ("coordinator_op_key", "coord-wrong"),
        ("priority", "critical"),
        ("server_generated_id", True),
        ("assigned_to", "attacker"),
        ("assigned_to_agent_id", "AI-attacker"),
        ("assigned_to_kind", "agent_name"),
    ],
)
def test_existing_intake_ticket_mismatch_is_collision(
    monkeypatch: pytest.MonkeyPatch, field: str, bad_value: Any
) -> None:
    draft = coordinator.deterministic_intake_draft(
        coordinator.IntakeAsk(
            "ask-collision", "Update the README guide", "operator", "board-a"
        ),
        _intake_project(),
    )
    existing = _matching_replay_ticket(draft)
    existing[field] = bad_value
    _install_replay_client(monkeypatch, existing)

    with pytest.raises(RuntimeError, match="intake idempotency collision"):
        asyncio.run(
            coordinator.create_intake_ticket(
                "https://board.invalid/mcp",
                "opaque",
                "coordinator-test",
                "board-a",
                draft,
            )
        )


@pytest.mark.parametrize("field", list(_matching_replay_ticket(
    coordinator.deterministic_intake_draft(
        coordinator.IntakeAsk("ask-fields", "Update the README guide", "operator", "board-a"),
        _intake_project(),
    )
)))
def test_existing_intake_ticket_missing_field_is_collision(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    draft = coordinator.deterministic_intake_draft(
        coordinator.IntakeAsk(
            "ask-missing", "Update the README guide", "operator", "board-a"
        ),
        _intake_project(),
    )
    existing = _matching_replay_ticket(draft)
    existing.pop(field)
    _install_replay_client(monkeypatch, existing)

    with pytest.raises(RuntimeError, match="intake idempotency collision"):
        asyncio.run(
            coordinator.create_intake_ticket(
                "https://board.invalid/mcp",
                "opaque",
                "coordinator-test",
                "board-a",
                draft,
            )
        )


def test_intake_collision_failure_keeps_queue_and_never_reports_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _intake_row("ask-collision", "Update the README guide")
    draft = coordinator.deterministic_intake_draft(
        coordinator.IntakeAsk(
            row["id"], row["text"], row["requested_by"], row["board_id"]
        ),
        _intake_project(),
    )
    existing = _matching_replay_ticket(draft)
    existing["origin"] = "ordinary-ticket"
    existing["assigned_to"] = "attacker"
    _install_replay_client(monkeypatch, existing)

    async def replay_create(board_id: str, candidate: coordinator.IntakeDraft) -> str:
        return await coordinator.create_intake_ticket(
            "https://board.invalid/mcp",
            "opaque",
            "coordinator-test",
            board_id,
            candidate,
        )

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state([row]),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=replay_create,
        )
    )

    assert [item["kind"] for item in findings] == ["intake-create-failed"]
    assert findings[0]["error_class"] == "RuntimeError"
    assert updates == {}


def test_intake_retry_uses_one_deterministic_ticket_identity() -> None:
    row = _intake_row("ask-crash", "Fix bug; steps to reproduce: run failing example")
    snapshot = {
        "board-a": {
            "tickets": [],
            "coordinator_intake_state": _intake_state([row]),
        }
    }
    created: set[str] = set()
    calls: list[str] = []

    async def create(_board_id: str, draft: coordinator.IntakeDraft) -> str:
        calls.append(draft.ticket_id)
        created.add(draft.ticket_id)
        return draft.ticket_id

    runtime = coordinator.RuntimeState.for_mode("active")
    first = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()], snapshot, NOW, runtime,
            enabled=True, dry_run=False, create_ticket=create,
        )
    )
    # Simulate a crash after ticket creation but before applying the empty queue.
    second = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()], snapshot, NOW, runtime,
            enabled=True, dry_run=False, create_ticket=create,
        )
    )

    assert len(calls) == 2
    assert len(created) == 1
    assert first[1] == second[1] == {"board-a": frozenset({"ask-crash"})}
    assert first[0][0]["op_key"] == second[0][0]["op_key"]


def test_intake_rate_limit_converts_auto_to_bounded_pending_draft() -> None:
    row = _intake_row("ask-docs", "Update the README documentation")
    tickets = [
        {
            "ticket_id": f"TK-{index}",
            "tags": ["coordinator-intake"],
            "created_at": ago(index + 1),
        }
        for index in range(5)
    ]

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("rate-limited intake must not create")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {"board-a": {"tickets": tickets, "coordinator_intake_state": _intake_state([row])}},
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=unexpected_create,
        )
    )

    assert findings[0]["kind"] == "intake-pending"
    assert findings[0]["matrix_rule"] == "hourly-auto-create-limit"
    assert len(findings[0]["evidence"]) <= coordinator.MAX_INTAKE_EVIDENCE_CHARS
    assert "remains queued" in findings[0]["next_action"]
    assert updates == {}


def test_intake_breaker_enters_draft_only_after_three_create_failures() -> None:
    rows = [
        _intake_row(f"ask-{index}", f"Update docs page {index}")
        for index in range(4)
    ]

    async def fail_create(*_args: Any) -> str:
        raise RuntimeError("simulated")

    runtime = coordinator.RuntimeState.for_mode("active")
    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {"board-a": {"tickets": [], "coordinator_intake_state": _intake_state(rows)}},
            NOW,
            runtime,
            enabled=True,
            dry_run=False,
            create_ticket=fail_create,
        )
    )

    assert "board-a" in runtime.intake_breakers
    assert sum(item["kind"] == "intake-create-failed" for item in findings) == 3
    assert any(item.get("matrix_rule") == "create-breaker-draft-only" for item in findings)
    assert all(
        "remains queued" in item["next_action"]
        for item in findings
        if item["kind"] == "intake-pending"
    )
    assert updates == {}


def test_intake_dry_run_shows_auto_and_ask_without_mutations() -> None:
    rows = [
        _intake_row("ask-auto", "Write documentation for the coordinator"),
        _intake_row("ask-ask", "Deploy and publish a release"),
    ]

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("dry-run must not create")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {"board-a": {"tickets": [], "coordinator_intake_state": _intake_state(rows)}},
            NOW,
            coordinator.RuntimeState.for_mode("shadow"),
            enabled=True,
            dry_run=True,
            create_ticket=unexpected_create,
        )
    )

    assert {item["kind"] for item in findings} == {
        "intake-would-create",
        "intake-pending",
    }
    assert updates == {}


def test_active_ask_remains_queued_and_reported_across_two_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _intake_row("ask-release", "Publish the next release")
    snapshot = {
        "board-a": {
            "tickets": [],
            "coordinator_intake_state": _intake_state([row]),
        }
    }
    published: list[dict[str, Any]] = []

    class FakeBoardClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeBoardClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def board_state_update(self, key: str, value: str) -> None:
            assert key == coordinator.STATE_KEY
            published.append(json.loads(value))

        async def _call(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("pending ASK must not drain coordinator_intake")

        async def memory_write(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("digest markers should suppress memory writes")

    fake_module = type("FakePursersClient", (), {"BoardClient": FakeBoardClient})
    monkeypatch.setitem(sys.modules, "pursers_client", fake_module)
    previous = {
        "board-a": {
            "last_daily_digest": NOW.date().isoformat(),
            "last_weekly_digest": (
                f"{NOW.isocalendar().year}-W{NOW.isocalendar().week:02d}"
            ),
        }
    }

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("policy ASK must not create")

    for _cycle in range(2):
        findings, updates = asyncio.run(
            coordinator.process_intakes(
                [_intake_project()],
                snapshot,
                NOW,
                coordinator.RuntimeState.for_mode("active"),
                enabled=True,
                dry_run=False,
                create_ticket=unexpected_create,
            )
        )
        assert updates == {}
        state = coordinator.bound_findings_state(findings, NOW)
        state.update(previous["board-a"])
        asyncio.run(
            coordinator.write_reports(
                "https://board.invalid/mcp",
                "opaque",
                "board-a",
                "coordinator-test",
                {"board-a": state},
                previous,
                NOW,
                updates,
            )
        )

    assert len(published) == 2
    assert all(
        [item["kind"] for item in state["findings"]] == ["intake-pending"]
        for state in published
    )


def test_bounded_state_reserves_intake_record_before_other_warnings() -> None:
    ordinary = [
        coordinator._finding(
            "closed-but-unmerged",
            "warn",
            "board-a",
            "x" * 300,
            ticket_id=f"TK-{index}",
            commit_hash="a" * 40,
        )
        for index in range(20)
    ]
    ask = coordinator.IntakeAsk("ask-reserved", "Update docs", "operator", "board-a")
    draft = coordinator.deterministic_intake_draft(ask, _intake_project())
    intake = coordinator.intake_finding(
        "intake-would-create",
        "info",
        ask,
        draft,
        "auto",
        "personal-docs-auto",
    )
    state = coordinator.bound_findings_state(
        [*ordinary, intake], NOW, max_chars=2_500
    )
    assert any(item.get("ask_id") == "ask-reserved" for item in state["findings"])


def test_intake_evidence_remains_valid_actionable_json_after_bounding() -> None:
    ask = coordinator.IntakeAsk(
        "ask-evidence", "Implement a new API endpoint with tests", "operator", "board-a"
    )
    draft = coordinator.deterministic_intake_draft(ask, _intake_project())
    finding = coordinator.intake_finding(
        "intake-pending",
        "warn",
        ask,
        draft,
        "ask",
        "personal-production-code-always-ask",
    )
    state = coordinator.bound_findings_state([finding], NOW)
    payload = json.loads(state["findings"][0]["evidence"])
    bounded_draft = payload["draft"]
    assert bounded_draft["title"] == draft.title
    assert bounded_draft["description"] == draft.description
    assert bounded_draft["scope"] == draft.scope
    assert bounded_draft["target_url"] == draft.target_url
    assert bounded_draft["required_fields"] == list(draft.required_fields)
    assert bounded_draft["coordinator_op_key"] == draft.op_key


def test_intake_without_grant_degrades_auto_to_draft_and_keeps_queue() -> None:
    row = _intake_row("ask-no-grant", "Update the README documentation")

    async def unexpected_create(*_args: Any) -> str:
        raise AssertionError("missing board:intake must not attempt creation")

    findings, updates = asyncio.run(
        coordinator.process_intakes(
            [_intake_project()],
            {
                "board-a": {
                    "tickets": [],
                    "coordinator_intake_state": _intake_state([row]),
                }
            },
            NOW,
            coordinator.RuntimeState.for_mode("active"),
            enabled=True,
            dry_run=False,
            create_ticket=unexpected_create,
            intake_authorized=False,
        )
    )
    assert findings[0]["kind"] == "intake-pending"
    assert findings[0]["matrix_rule"] == "missing-board-intake-grant"
    assert "Grant board:intake" in findings[0]["next_action"]
    assert updates == {}


def test_capability_scopes_reads_jwt_hint_without_trusting_it() -> None:
    def encode(value: dict[str, Any]) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    assert coordinator.capability_scopes(
        encode({"scope": "board:read board:coordinate board:intake"})
    ) == frozenset({"board:read", "board:coordinate", "board:intake"})
    assert coordinator.capability_scopes("opaque") == frozenset()


def test_intake_is_disabled_by_default(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("opaque", encoding="utf-8")
    default = coordinator.parse_args(["--token-path", str(token), "--once"])
    enabled = coordinator.parse_args(
        ["--token-path", str(token), "--once", "--enable-intake"]
    )
    disabled = coordinator.parse_args(
        ["--token-path", str(token), "--once", "--disable-intake"]
    )
    assert default.intake_enabled is False
    assert enabled.intake_enabled is True
    assert disabled.intake_enabled is False


def test_intake_queue_is_drained_only_after_finding_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pursers_client

    calls: list[tuple[str, str]] = []

    class FakeBoardClient:
        agent_name = "coordinator-test"

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeBoardClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def board_state_update(self, key: str, value: str) -> dict[str, bool]:
            calls.append((key, value))
            return {"ok": True}

        async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, bool]:
            assert name == "board_state_update"
            assert arguments["agent_name"] == "coordinator-test"
            raw = json.dumps(
                [
                    _intake_row("ask-done", "Update docs"),
                    _intake_row("ask-appended", "Add tests"),
                ]
            )
            assert arguments["expected_sha256"] == hashlib.sha256(
                raw.encode()
            ).hexdigest()
            calls.append((arguments["key"], arguments["value"]))
            return {"ok": True}

        async def board_state_get(self, key: str) -> dict[str, Any]:
            assert key == coordinator.INTAKE_STATE_KEY
            return _intake_state(
                [
                    _intake_row("ask-done", "Update docs"),
                    _intake_row("ask-appended", "Add tests"),
                ]
            )

        async def memory_write(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("digest markers should suppress memory writes")

    monkeypatch.setattr(pursers_client, "BoardClient", FakeBoardClient)
    state = coordinator.bound_findings_state([], NOW)
    state["last_daily_digest"] = NOW.date().isoformat()
    state["last_weekly_digest"] = (
        f"{NOW.isocalendar().year}-W{NOW.isocalendar().week:02d}"
    )
    asyncio.run(
        coordinator.write_reports(
            "https://board.invalid/mcp",
            "opaque",
            "board-a",
            "coordinator-test",
            {"board-a": state},
            {
                "board-a": {
                    "last_daily_digest": NOW.date().isoformat(),
                    "last_weekly_digest": (
                        f"{NOW.isocalendar().year}-W{NOW.isocalendar().week:02d}"
                    ),
                }
            },
            NOW,
            {"board-a": frozenset({"ask-done"})},
        )
    )
    assert [key for key, _value in calls] == [
        coordinator.STATE_KEY,
        coordinator.INTAKE_STATE_KEY,
    ]
    drained = json.loads(calls[1][1])
    assert drained["schema_version"] == coordinator.INTAKE_DOCUMENT_SCHEMA_VERSION
    assert [item["id"] for item in drained["asks"]] == ["ask-appended"]
    assert drained["tombstones"] == []


def test_intake_drain_preserves_decline_tombstones() -> None:
    tombstone = {
        "id": "ask-declined",
        "text": "Declined ask",
        "board_id": "board-a",
        "declined_by": "operator",
        "declined_at": NOW.isoformat(),
    }
    value = coordinator._serialize_intake(
        [
            coordinator.IntakeAsk(
                "ask-done", "Update docs", "operator", "board-a"
            )
        ],
        [tombstone],
    )

    class Client:
        agent_name = "coordinator-test"
        written: str | None = None

        async def board_state_get(self, key: str) -> dict[str, Any]:
            assert key == coordinator.INTAKE_STATE_KEY
            return {"state": {"value": value}}

        async def _call(self, name: str, arguments: dict[str, Any]) -> None:
            assert name == "board_state_update"
            assert arguments["expected_sha256"] == hashlib.sha256(
                value.encode()
            ).hexdigest()
            self.written = arguments["value"]

    client = Client()
    asyncio.run(
        coordinator.drain_intake(client, "board-a", frozenset({"ask-done"}))
    )
    assert client.written is not None
    document = json.loads(client.written)
    assert document["asks"] == []
    assert document["tombstones"] == [tombstone]


def test_intake_cas_drain_rejects_append_between_read_and_write() -> None:
    initial = [_intake_row("ask-done", "Update docs")]
    appended = _intake_row("ask-appended", "Add tests")

    class RacingClient:
        agent_name = "coordinator-test"

        def __init__(self) -> None:
            self.value = json.dumps(initial)

        async def board_state_get(self, key: str) -> dict[str, Any]:
            assert key == coordinator.INTAKE_STATE_KEY
            observed = self.value
            self.value = json.dumps([*initial, appended])
            return {"state": {"value": observed}}

        async def _call(self, name: str, arguments: dict[str, Any]) -> None:
            assert name == "board_state_update"
            if arguments["expected_sha256"] != hashlib.sha256(
                self.value.encode()
            ).hexdigest():
                raise RuntimeError("state precondition failed")
            self.value = arguments["value"]

    client = RacingClient()
    with pytest.raises(RuntimeError, match="state precondition failed"):
        asyncio.run(
            coordinator.drain_intake(
                client, "board-a", frozenset({"ask-done"})
            )
        )
    assert [item["id"] for item in json.loads(client.value)] == [
        "ask-done",
        "ask-appended",
    ]


def _config_state(value: dict[str, Any]) -> dict[str, Any]:
    return {"state": {"value": json.dumps(value)}}


def test_live_config_precedence_and_invalid_field_fallback(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("opaque", encoding="utf-8")
    args = coordinator.parse_args(
        [
            "--token-path", str(token), "--stale-seconds", "444",
            "--starved-seconds", "555", "--intake-rate-per-hour", "9",
        ]
    )
    document = {
        "schema_version": 1,
        "thresholds": {
            "stale_seconds": 111,
            "lease_warning_ratio": 0.5,
            "grace_seconds": 222,
            "starved_seconds": 1,  # Invalid: explicit flag must win.
            "critical_starved_seconds": 333,
            "review_backlog_seconds": 444,
            "abandoner_drops": 4,
            "abandoner_window_days": 8,
        },
        "integration_watch_since": None,
        "intake": {
            "enabled": True,
            "auto_categories": list(coordinator.DEFAULT_AUTO_CATEGORIES),
            "always_ask_categories": list(coordinator.DEFAULT_ALWAYS_ASK_CATEGORIES),
            "work_domain_always_ask": True,
            "rate_per_hour": 7,
        },
    }

    resolved = coordinator.resolve_coordinator_config(_config_state(document), args)

    assert resolved.thresholds.stale_seconds == 111
    assert resolved.sources["thresholds.stale_seconds"] == "config"
    assert resolved.thresholds.starved_seconds == 555
    assert resolved.sources["thresholds.starved_seconds"] == "flag"
    assert resolved.rate_per_hour == 7
    assert "thresholds.starved_seconds" in resolved.invalid_fields
    finding = coordinator.config_invalid_finding("pursers", resolved)
    assert finding and finding["kind"] == "config-invalid"


def test_missing_config_uses_builtins_without_invalid_finding(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("opaque", encoding="utf-8")
    args = coordinator.parse_args(["--token-path", str(token)])

    resolved = coordinator.resolve_coordinator_config(None, args)

    assert resolved.thresholds == coordinator.Thresholds()
    assert set(resolved.sources.values()) == {"default"}
    assert coordinator.config_invalid_finding("pursers", resolved) is None


def test_parse_args_none_tracks_explicit_process_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("opaque", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coordinator.py", "--token-path", str(token),
            "--stale-seconds", "999", "--enable-intake",
            "--intake-rate-per-hour", "9",
        ],
    )
    args = coordinator.parse_args(None)
    document = {
        "schema_version": 1,
        "thresholds": {
            "stale_seconds": 1,
            "lease_warning_ratio": 0.8,
            "grace_seconds": 600,
            "starved_seconds": 1800,
            "critical_starved_seconds": 600,
            "review_backlog_seconds": 1800,
            "abandoner_drops": 3,
            "abandoner_window_days": 7,
        },
        "integration_watch_since": None,
        "intake": {
            "auto_categories": list(coordinator.DEFAULT_AUTO_CATEGORIES),
            "always_ask_categories": list(coordinator.DEFAULT_ALWAYS_ASK_CATEGORIES),
            "work_domain_always_ask": True,
            "rate_per_hour": 99,
        },
    }

    resolved = coordinator.resolve_coordinator_config(_config_state(document), args)

    assert resolved.thresholds.stale_seconds == 999
    assert resolved.intake_enabled is True
    assert resolved.rate_per_hour == 9
    assert resolved.sources["thresholds.stale_seconds"] == "flag"
    assert resolved.sources["intake.enabled"] == "flag"
    assert resolved.sources["intake.rate_per_hour"] == "flag"
