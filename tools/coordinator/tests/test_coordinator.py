from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
            {"agent_id": "AI-busy", "agent_name": "worker-a", "last_activity_at": ago(10), "status": "working"},
            {"agent_id": "AI-free", "agent_name": "worker-b", "last_activity_at": ago(20), "status": "active"},
        ],
        "tickets": [{"ticket_id": "TK-old", "status": "open", "priority": "medium", "created_at": ago(3_601)}],
    }
    finding = coordinator.ticket_findings("board-a", snapshot, NOW)[0]
    assert finding["escalation_stage"] == 2
    assert finding["would_assign_to_agent_id"] == "AI-free"


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
    assert history == []
    assert uncertainty == [
        {
            "ticket_id": "TK-baseline",
            "observed_at": NOW.isoformat(),
            "count": 3,
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
    assert len(history2) == 1 and history2[0]["count"] == 1
    assert uncertainty2[0]["count"] == 2


def test_multi_count_delta_is_not_attributed_to_latest_holder() -> None:
    ticket = {
        "ticket_id": "TK-multi",
        "abandoned_count": 3,
        "last_abandoned_by": "AI-latest-only",
        "last_abandoned_at": ago(10),
    }
    findings, _, history, uncertainty = coordinator.update_drop_evidence(
        "board-a", [ticket], {"drop_counters": {"TK-multi": 0}}, NOW
    )
    assert history == []
    assert uncertainty[0]["count"] == 3
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
