from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "board_butler.py"
REPOSITORY_ROOT = MODULE_PATH.parents[2]
SPEC = importlib.util.spec_from_file_location("board_butler", MODULE_PATH)
assert SPEC and SPEC.loader
butler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = butler
SPEC.loader.exec_module(butler)


NOW = butler.datetime(2026, 9, 16, 12, 0, tzinfo=butler.timezone.utc)


class Source:
    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.agents: list[dict[str, Any]] = []

    async def ticket_get(self, ticket_id: str) -> Mapping[str, Any]:
        return {"ticket": self.tickets[ticket_id]}

    async def board_status(self) -> Mapping[str, Any]:
        return {"agents": self.agents}


def question(message: str, *, kind: str = "information") -> dict[str, str]:
    return {
        "board_id": "pursers",
        "ticket_id": "TK-source",
        "question_id": "CQ-source",
        "kind": kind,
        "message": message,
    }


def args(tmp_path: Path, *, dry_run: bool = False) -> argparse.Namespace:
    token = tmp_path / "token.jwt"
    token.write_text("opaque", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    return argparse.Namespace(
        url="https://central.invalid/mcp",
        token_path=token,
        home_board="pursers",
        agent_name="board-butler-test",
        repo=repo,
        integration_ref="origin/main",
        pid_file=tmp_path / "butler.pid",
        cursor_file=tmp_path / "cursor.json",
        drafts_per_hour=5,
        drafts_per_ticket=2,
        wait_timeout=1,
        once=True,
        dry_run=dry_run,
    )


@pytest.mark.parametrize(
    ("message", "kind", "expected", "rule"),
    [
        ("Please waive the failed release gate", "information", "ESCALATE", "gate-waiver"),
        ("Expand the ticket scope to include the service", "information", "ESCALATE", "scope-change"),
        ("Should we publish and tag this release?", "information", "ESCALATE", "release-decision"),
        ("Register this new board in the project registry", "information", "ESCALATE", "membership-or-registry"),
        ("Is abcdef1 an ancestor of origin/main?", "information", "MECHANICAL", "git-ancestry"),
        ("Is abcdef1 an ancestor of origin/main?", "decision", "MECHANICAL", "git-ancestry"),
        ("What is the status of TK-123?", "information", "MECHANICAL", "ticket-status"),
        ("May we waive the gate because abcdef1 is merged?", "information", "ESCALATE", "gate-waiver"),
        ("Is this okay?", "approval", "ESCALATE", "question-kind:approval"),
        ("Does this look fine?", "information", "UNKNOWN", "no-confident-policy-match"),
    ],
)
def test_policy_table_is_explicit_and_escalation_first(
    message: str, kind: str, expected: str, rule: str
) -> None:
    result = butler.classify_question(message, kind)
    assert result.outcome.value == expected
    assert result.rule == rule


def test_every_policy_rule_has_a_readable_name_and_pattern() -> None:
    assert len({rule.name for rule in butler.POLICY_TABLE}) == len(butler.POLICY_TABLE)
    assert all(rule.name and rule.pattern.pattern for rule in butler.POLICY_TABLE)
    first_mechanical = next(
        index
        for index, rule in enumerate(butler.POLICY_TABLE)
        if rule.outcome is butler.Outcome.MECHANICAL
    )
    assert all(
        rule.outcome is butler.Outcome.ESCALATE
        for rule in butler.POLICY_TABLE[:first_mechanical]
    )
    assert butler.COORDINATOR_ALWAYS_ASK_CATEGORIES == (
        "production-code",
        "release-ci",
        "membership-roles",
        "board-registry",
    )


def test_ticket_status_draft_cites_product_source(tmp_path: Path) -> None:
    source = Source()
    source.tickets["TK-123"] = {"ticket_id": "TK-123", "status": "closed"}
    finding = asyncio.run(
        butler.make_finding(
            question("What is the status of TK-123?"),
            source,
            tmp_path,
            "origin/main",
            NOW,
        )
    )

    assert finding["kind"] == "would_answer"
    assert finding["verdict"] == "MECHANICAL"
    assert finding["message"] == "TK-123 is closed."
    assert finding["evidence"].startswith("source=Central ticket_get(TK-123)")


def test_missing_mechanical_evidence_fails_closed_to_unknown(tmp_path: Path) -> None:
    finding = asyncio.run(
        butler.make_finding(
            question("Is the mentioned commit an ancestor of origin/main?"),
            Source(),
            tmp_path,
            "origin/main",
            NOW,
        )
    )

    assert finding["verdict"] == "UNKNOWN"
    assert "incomplete" in finding["message"]
    assert "evidence_error=ValueError" in finding["evidence"]


def test_real_tk_1ec_submission_escalates_when_covering_aionui_suite_failed() -> None:
    source = Source()
    # Sanitized from product ticket TK-1ec2ca97709b latest submission metadata and
    # AN-000000001054.  This is the regression that earned the binding amendment.
    source.tickets["TK-1ec2ca97709b"] = {
        "notes": """\
cumulative_files_changed: ["packages/central/README.md","packages/central/src/pursers_central/central.py","packages/client/src/pursers_client/__init__.py","packages/client/src/pursers_client/request_state.py","packages/client/tests/test_request_state.py","tools/wait-bridge/README.md","tools/wait-bridge/pursers_wait_server.py","tools/wait-bridge/tests/test_human_requests.py","tools/wait-bridge/tests/test_mrtr_protocol.py"]
test-command: python3 tools/ci_manifest.py run
test-output: central 209 passed; client 98 passed; wait-bridge 297 passed; AionUi: 52 failed, 229 passed, 3 skipped; every failure is sandbox denial of /bin/ps
"""
    }

    finding = asyncio.run(
        butler.make_finding(
            question(
                "May review proceed when the AionUi suite failed for TK-1ec2ca97709b?",
                kind="decision",
            ),
            source,
            REPOSITORY_ROOT,
            "origin/main",
            NOW,
        )
    )

    assert finding["verdict"] == "ESCALATE"
    assert finding["policy_rule"] == "coverage-blindness"
    assert "packages/client/src/pursers_client/__init__.py -> aionui-extension" in finding["message"]
    assert "tools/ci_manifest.py" in finding["evidence"]


def test_blocked_suite_unrelated_to_docs_only_diff_is_mechanical() -> None:
    source = Source()
    source.tickets["TK-docs"] = {
        "notes": """\
cumulative_files_changed: ["docs/design-home/example.md"]
test-command: python3 tools/ci_manifest.py run
test-output: AionUi: 52 failed, 229 passed, 3 skipped; sandbox denied /bin/ps
"""
    }

    finding = asyncio.run(
        butler.make_finding(
            question(
                "May review proceed when the AionUi suite failed for TK-docs?",
                kind="decision",
            ),
            source,
            REPOSITORY_ROOT,
            "origin/main",
            NOW,
        )
    )

    assert finding["verdict"] == "MECHANICAL"
    assert "do not cover the submitted diff" in finding["message"]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("AionUi: 3 skipped", "skipped"),
        ("AionUi: 0 passed, 3 skipped", "skipped"),
        ("AionUi: 229 passed, 3 skipped", "passed"),
        ("AionUi: command timed out", "failed"),
        ("AionUi: execution evidence unavailable", "never-reached"),
    ],
)
def test_suite_status_does_not_treat_skip_counts_as_passes(
    output: str, expected: str
) -> None:
    assert butler._suite_statuses(output, ["aionui-extension"]) == {
        "aionui-extension": expected
    }


def test_git_ancestry_draft_consumes_real_repository_state(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "one"], check=True)
    first = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "two"], check=True)

    finding = asyncio.run(
        butler.make_finding(
            question(f"Is {first} an ancestor of origin/main?"),
            Source(),
            tmp_path,
            "HEAD",
            NOW,
        )
    )

    assert finding["verdict"] == "MECHANICAL"
    assert finding["message"] == f"{first} is an ancestor of HEAD."
    assert "git merge-base --is-ancestor" in finding["evidence"]


def test_annotation_and_capability_drafts_cite_named_sources(tmp_path: Path) -> None:
    source = Source()
    source.tickets["TK-123"] = {
        "ticket_id": "TK-123",
        "annotations": [
            {"annotation_id": "AN-9", "kind": "decision", "text": "Covers failure X."}
        ],
    }
    source.agents = [
        {
            "agent_id": "AI-1",
            "agent_name": "worker-1",
            "role": "worker",
            "lifecycle_status": "active",
            "capabilities": {"can_work": True, "can_review": False},
        }
    ]

    annotation = asyncio.run(
        butler.make_finding(
            question("Does AN-9 on TK-123 cover this decision?"),
            source,
            tmp_path,
            "origin/main",
            NOW,
        )
    )
    capability = asyncio.run(
        butler.make_finding(
            question("Is seat `worker-1` capable of can_work?"),
            source,
            tmp_path,
            "origin/main",
            NOW,
        )
    )

    assert "annotations[AN-9]" in annotation["evidence"]
    assert "board_snapshot.agents[worker-1]" in capability["evidence"]
    assert annotation["verdict"] == capability["verdict"] == "MECHANICAL"


def test_identity_rejects_shared_worker_or_reviewer_principal() -> None:
    identity = SimpleNamespace(
        agent_id="AI-butler", principal_id="PR-shared", role="coordinator"
    )
    agents = [
        {
            "agent_id": "AI-butler",
            "agent_name": "board-butler-1",
            "principal_id": "PR-shared",
            "role": "coordinator",
            "capabilities": {"can_work": False, "can_review": False},
        },
        {
            "agent_id": "AI-worker",
            "agent_name": "worker-1",
            "principal_id": "PR-shared",
            "capabilities": {"can_work": True, "can_review": False},
        }
    ]
    with pytest.raises(butler.IdentityConflict, match="also works or reviews"):
        butler.assert_independent_identity(identity, agents)


def test_identity_accepts_distinct_non_working_principal() -> None:
    identity = SimpleNamespace(
        agent_id="AI-butler", principal_id="PR-butler", role="coordinator"
    )
    butler.assert_independent_identity(
        identity,
        [
            {
                "agent_id": "AI-butler",
                "principal_id": "PR-butler",
                "role": "coordinator",
                "capabilities": {"can_work": False, "can_review": False},
            },
            {
                "agent_id": "AI-worker",
                "principal_id": "PR-worker",
                "capabilities": {"can_work": True, "can_review": False},
            }
        ],
    )


def test_identity_rejects_active_worker_role_even_with_disabled_caps() -> None:
    identity = SimpleNamespace(
        agent_id="AI-butler", principal_id="PR-shared", role="coordinator"
    )
    agents = [
        {
            "agent_id": "AI-butler",
            "principal_id": "PR-shared",
            "role": "coordinator",
            "capabilities": {"can_work": False, "can_review": False},
        },
        {
            "agent_id": "AI-worker",
            "principal_id": "PR-shared",
            "role": "worker",
            "lifecycle_status": "active",
            "capabilities": {"can_work": False, "can_review": False},
        },
    ]
    with pytest.raises(butler.IdentityConflict, match="also works or reviews"):
        butler.assert_independent_identity(identity, agents)


def test_identity_fails_closed_on_truncated_agent_view() -> None:
    with pytest.raises(butler.IdentityConflict, match="truncated agent view"):
        butler.assert_complete_agent_view(
            {"agents": [], "omitted_counts": {"agents": 1}}
        )


def test_singleton_second_instance_exits_before_board_access(tmp_path: Path) -> None:
    options = args(tmp_path)
    constructed = 0

    def backend_factory(*_args: Any) -> Any:
        nonlocal constructed
        constructed += 1
        raise AssertionError("board backend must not be constructed")

    with butler.SingletonLock(options.pid_file):
        with pytest.raises(butler.AlreadyRunning):
            asyncio.run(butler.run(options, backend_factory=backend_factory))

    assert constructed == 0


def test_quiet_once_has_one_push_wait_and_zero_central_writes(tmp_path: Path) -> None:
    options = args(tmp_path)

    class QuietBackend:
        latest_seq = 44
        waits = 0
        writes = 0
        reads = 0

        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> "QuietBackend":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def wait_for_question(self, cursor: int, _timeout: float) -> tuple[int, None]:
            assert cursor == 44
            self.waits += 1
            return cursor, None

    backend = QuietBackend()
    asyncio.run(butler.run(options, backend_factory=lambda *_args: backend))

    assert backend.waits == 1
    assert backend.reads == 0
    assert backend.writes == 0
    assert json.loads(options.cursor_file.read_text())["cursor"] == 44


def test_cursor_zero_is_not_reused_for_catchup(tmp_path: Path) -> None:
    cursor = tmp_path / "cursor.json"
    cursor.write_text('{"cursor": 0}\n', encoding="utf-8")

    assert butler.load_cursor(cursor) is None


def test_closed_resident_push_stream_does_not_reconnect_spin(tmp_path: Path) -> None:
    options = args(tmp_path)
    options.once = False

    class ClosedBackend:
        latest_seq = 44
        waits = 0

        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> "ClosedBackend":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def wait_for_question(
            self, cursor: int, _timeout: None
        ) -> tuple[int, None]:
            self.waits += 1
            return cursor, None

    backend = ClosedBackend()
    with pytest.raises(RuntimeError, match="push subscription ended"):
        asyncio.run(butler.run(options, backend_factory=lambda *_args: backend))

    assert backend.waits == 1


def test_dry_run_prints_draft_and_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    options = args(tmp_path, dry_run=True)

    class Backend(Source):
        writes = 0

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            return {}

        async def write_findings(self, *_args: Any) -> None:
            self.writes += 1

    backend = Backend()
    backend.tickets["TK-123"] = {"status": "submitted"}
    asyncio.run(
        butler.process_question(
            backend,
            question("What is the status of TK-123?"),
            options,
            NOW,
        )
    )

    assert backend.writes == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "would_answer"


def test_duplicate_question_is_idempotent_and_does_not_write(tmp_path: Path) -> None:
    options = args(tmp_path)
    existing = {
        "kind": "would_answer",
        "ticket_id": "TK-source",
        "question_id": "CQ-source",
        "verdict": "MECHANICAL",
        "observed_at": NOW.isoformat(),
    }

    class Backend(Source):
        writes = 0

        async def findings(self) -> Mapping[str, Any]:
            return {
                "state": {
                    "value": json.dumps(
                        {"schema_version": 2, "findings": [existing]}
                    )
                }
            }

        async def coordinator_config(self) -> Mapping[str, Any]:
            raise AssertionError("duplicate must return before extra reads")

        async def write_findings(self, *_args: Any) -> None:
            self.writes += 1

    backend = Backend()
    result = asyncio.run(
        butler.process_question(backend, question("anything"), options, NOW)
    )

    assert result == existing
    assert backend.writes == 0


def test_cursor_is_not_committed_before_finding_write(tmp_path: Path) -> None:
    options = args(tmp_path)

    class FailingBackend(Source):
        latest_seq = 10

        async def __aenter__(self) -> "FailingBackend":
            self.tickets["TK-123"] = {"status": "closed"}
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def wait_for_question(
            self, cursor: int, _timeout: float
        ) -> tuple[int, Mapping[str, Any]]:
            assert cursor == 10
            return 11, question("What is the status of TK-123?")

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            return {}

        async def write_findings(self, *_args: Any) -> None:
            raise RuntimeError("simulated CAS failure")

    backend = FailingBackend()
    with pytest.raises(RuntimeError, match="simulated CAS failure"):
        asyncio.run(butler.run(options, backend_factory=lambda *_args: backend))

    assert not options.cursor_file.exists()


def test_rate_limits_are_configurable_and_reported() -> None:
    state = {
        "findings": [
            {
                "kind": "would_answer",
                "ticket_id": "TK-source",
                "question_id": f"CQ-{index}",
                "observed_at": NOW.isoformat(),
            }
            for index in range(2)
        ]
    }
    assert butler.rate_limit_reason(state, "TK-source", NOW, 5, 2) == "per_ticket"
    assert butler.rate_limit_reason(state, "TK-other", NOW, 2, 5) == "per_hour"
    finding = butler.rate_limit_finding(question("anything"), "per_hour", NOW)
    assert finding["kind"] == "butler_rate_limited"
    assert finding["evidence"] == "source=board_butler_rate_limit; limit=per_hour"


def test_rate_limit_reuses_coordinator_intake_shape(tmp_path: Path) -> None:
    options = args(tmp_path)
    assert butler.limits_from_config(
        {"intake": {"rate_per_hour": 7}}, options
    ) == (7, 2)
    assert butler.limits_from_config(
        {
            "intake": {"rate_per_hour": 7},
            "board_butler": {"drafts_per_hour": 3, "drafts_per_ticket": 1},
        },
        options,
    ) == (3, 1)


def test_findings_merge_is_bounded_and_dedupes_question_id() -> None:
    old = {
        "schema_version": 2,
        "findings": [
            {"kind": "would_answer", "question_id": "CQ-source", "message": "old"},
            {"kind": "starved", "ticket_id": "TK-other"},
        ],
        "truncation": {"findings": 0},
    }
    new = {
        "kind": "would_answer",
        "question_id": "CQ-source",
        "ticket_id": "TK-source",
        "verdict": "MECHANICAL",
    }
    merged = butler.merge_finding(old, new, NOW)
    assert sum(row.get("question_id") == "CQ-source" for row in merged["findings"]) == 1
    assert any(row.get("kind") == "starved" for row in merged["findings"])
    assert merged["board_butler"]["last_verdict"] == "MECHANICAL"


def test_findings_merge_drops_old_rows_to_stay_bounded() -> None:
    old = {
        "schema_version": 2,
        "findings": [
            {
                "kind": "starved",
                "level": "warn",
                "ticket_id": f"TK-{index}",
                "message": "x" * 400,
            }
            for index in range(20)
        ],
        "truncation": {"findings": 0},
    }
    new = {
        "kind": "would_answer",
        "question_id": "CQ-new",
        "ticket_id": "TK-new",
        "verdict": "MECHANICAL",
        "message": "new draft",
    }
    merged = butler.merge_finding(old, new, NOW)
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    assert len(encoded) <= butler.MAX_STATE_CHARS
    assert merged["findings"][-1]["question_id"] == "CQ-new"
    assert merged["truncation"]["findings"] > 0


def test_findings_merge_never_evicts_critical_coordinator_alerts() -> None:
    critical = {
        "kind": "privacy-leak-suspect",
        "level": "critical",
        "ticket_id": "TK-critical",
        "message": "must survive",
    }
    old = {
        "schema_version": 2,
        "findings": [critical]
        + [
            {
                "kind": "starved",
                "level": "warn",
                "ticket_id": f"TK-{index}",
                "message": "x" * 400,
            }
            for index in range(20)
        ],
        "truncation": {"findings": 0},
    }
    new = {
        "kind": "would_answer",
        "question_id": "CQ-new",
        "ticket_id": "TK-new",
        "verdict": "MECHANICAL",
        "message": "new draft",
    }

    merged = butler.merge_finding(old, new, NOW)

    assert critical in merged["findings"]
    assert merged["findings"][-1]["question_id"] == "CQ-new"


def test_findings_merge_refuses_to_displace_a_full_critical_set() -> None:
    old = {
        "schema_version": 2,
        "findings": [
            {
                "kind": "privacy-leak-suspect",
                "level": "critical",
                "ticket_id": f"TK-{index}",
            }
            for index in range(butler.MAX_FINDINGS)
        ],
        "truncation": {"findings": 0},
    }

    with pytest.raises(ValueError, match="no bounded room after critical alerts"):
        butler.merge_finding(
            old,
            {"kind": "would_answer", "question_id": "CQ-new"},
            NOW,
        )


def test_module_has_no_question_answer_or_ticket_mutation_path() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "ticket_question_" + "answer",
        "ticket_" + "submit",
        "ticket_" + "claim",
        "ticket_" + "update",
        "ticket_" + "annotate",
    )
    assert all(name not in source for name in forbidden)
    assert "board_catchup" not in source
    assert "ticket_list" not in source
