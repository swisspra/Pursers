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
BACKLOG_FIXTURE = (
    MODULE_PATH.parent
    / "tests"
    / "fixtures"
    / "coordinator_questions_2026-09-16.json"
)
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
        drafts_per_board=20,
        project="Pursers",
        wait_timeout=1,
        once=True,
        dry_run=dry_run,
        kill_switch=False,
        veto_question=None,
        control_reason="operator",
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
        (
            "Is abcdef1 merged into main, and may I merge it now?",
            "decision",
            "ESCALATE",
            "production-code-authority",
        ),
        (
            "Is abcdef1 contained in origin/main, and should I change production code to land it?",
            "decision",
            "ESCALATE",
            "production-code-authority",
        ),
        ("What is the status of TK-123?", "information", "MECHANICAL", "ticket-status"),
        ("May we waive the gate because abcdef1 is merged?", "information", "ESCALATE", "gate-waiver"),
        (
            "Please authorize carrying forward the independent replay result.",
            "decision",
            "ESCALATE",
            "gate-waiver",
        ),
        (
            "May I satisfy the acceptance with a product-builder replay?",
            "decision",
            "ESCALATE",
            "gate-waiver",
        ),
        (
            "Please accept this exact local verifier result instead.",
            "decision",
            "ESCALATE",
            "gate-waiver",
        ),
        (
            "AionUi suite reported 52 failures; may I submit after running release-tools?",
            "decision",
            "ESCALATE",
            "coverage-blindness",
        ),
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


def test_authoritative_backlog_replay_is_truthfully_partial() -> None:
    corpus = json.loads(BACKLOG_FIXTURE.read_text(encoding="utf-8"))
    available = corpus["available_records"]
    unavailable = corpus["unavailable_records"]
    all_ids = [row["question_id"] for row in available + unavailable]

    assert corpus["authoritative_count"] == 27
    assert len(available) == 3
    assert len(unavailable) == 24
    assert len(all_ids) == len(set(all_ids)) == 27
    assert all("message" not in row and "answer" not in row for row in unavailable)

    verdicts = {
        row["question_id"]: butler.classify_question(
            row["message"], row["kind"]
        ).outcome.value
        for row in available
    }
    assert verdicts == {
        row["question_id"]: row["expected_verdict"] for row in available
    }
    assert list(verdicts.values()).count("MECHANICAL") == 1
    assert list(verdicts.values()).count("ESCALATE") == 2
    assert list(verdicts.values()).count("UNKNOWN") == 0
    assert {
        row["question_id"]
        for row in available
        if row["expected_verdict"] != row["recorded_disposition"]
    } == {"CQ-53524d65cdb51016"}


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


@pytest.mark.parametrize(
    "message",
    [
        "Is abcdef1 merged into main, and may I merge it now?",
        "Is abcdef1 contained in origin/main, and should I change production code to land it?",
    ],
)
def test_authority_bearing_decision_short_circuits_ancestry_evaluator(
    tmp_path: Path, message: str
) -> None:
    finding = asyncio.run(
        butler.make_finding(
            question(message, kind="decision"),
            Source(),
            tmp_path,
            "origin/main",
            NOW,
        )
    )

    assert finding["verdict"] == "ESCALATE"
    assert finding["policy_rule"] == "production-code-authority"
    assert finding["evidence"].startswith("source=policy_table:production-code-authority")
    assert "git merge-base" not in finding["evidence"]


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
            {
                **question(
                    "Validation environment blocker: `python3 tools/ci_manifest.py run` "
                    "passes central (209), client (98), import (106), personal "
                    "(200/2 skipped), wait-bridge (296), fleet-dashboard (357), "
                    "coordinator (233), worker-runtime (106), acp-seat (28/1 "
                    "skipped), acp-agent (15), and seat-kit (116), then AionUi "
                    "typed-evidence hits 52 environment failures because sandbox "
                    "denies `/bin/ps` with `PermissionError: [Errno 1] Operation "
                    "not permitted`. Source-focused MCP 2.0.0 suite passes 40 "
                    "tests + 10 subtests. I cannot authorize unsandboxed execution. "
                    "Continuing leak/diff validation; please treat the full-manifest "
                    "failure as host-policy evidence or provide an authorized "
                    "non-sandboxed validator."
                ),
                "ticket_id": "TK-1ec2ca97709b",
                "question_id": "CQ-2ea9ba8fb16e245c",
            },
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


def test_dry_run_does_not_consume_question_cursor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = args(tmp_path, dry_run=True)

    class Backend(Source):
        latest_seq = 10
        writes = 0

        async def __aenter__(self) -> "Backend":
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
            self.writes += 1

    backend = Backend()
    asyncio.run(butler.run(options, backend_factory=lambda *_args: backend))

    assert backend.writes == 0
    assert not options.cursor_file.exists()
    assert json.loads(capsys.readouterr().out)["question_id"] == "CQ-source"


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


def test_process_question_reports_every_effective_value_and_durable_hold(
    tmp_path: Path,
) -> None:
    options = args(tmp_path)

    class Backend(Source):
        written: dict[str, Any] | None = None

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            return {
                "board_butler": {
                    "schema_version": 1,
                    "global": {
                        "mode": "active",
                        "kill_switch": False,
                        "answer_scope": {"ticket_status": "auto"},
                        "required_evidence_kinds": ["ticket_status"],
                        "ceilings": {
                            "per_hour": 4,
                            "per_ticket": 2,
                            "per_board": 8,
                        },
                        "hold_before_post_s": 300,
                        "active_windows": [
                            {
                                "days": ["wed"],
                                "start": "00:00",
                                "end": "23:59",
                                "timezone": "UTC",
                            }
                        ],
                        "auto_demote": {"veto_count": 2, "window_s": 1800},
                    },
                }
            }

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.written = json.loads(value)

    backend = Backend()
    backend.tickets["TK-123"] = {"status": "closed"}
    finding = asyncio.run(
        butler.process_question(
            backend,
            question("What is the status of TK-123?"),
            options,
            NOW,
        )
    )

    assert finding["auto_eligible"] is True
    assert finding["configured_action"] == "auto"
    assert finding["hold"]["status"] == "shadow"
    assert finding["hold"]["release_at"] == (
        NOW + butler.timedelta(seconds=300)
    ).isoformat()
    effective = finding["effective_config"]
    assert effective["effective_mode"] == "shadow"
    assert effective["future_active_state"] == "eligible"
    assert effective["ceilings"] == {
        "per_hour": 4,
        "per_ticket": 2,
        "per_board": 8,
    }
    assert set(effective) == {
        "schema_version",
        "configured_mode",
        "effective_mode",
        "future_active_state",
        "demotion_reason",
        "answer_scope",
        "required_evidence_kinds",
        "ceilings",
        "hold_before_post_s",
        "active_windows",
        "kill_switch",
        "auto_demote",
        "classification",
        "drafting",
        "source_layers",
        "precedence",
    }
    assert backend.written is not None
    assert backend.written["findings"][-1]["hold"] == finding["hold"]


def test_invalid_config_fails_closed_and_queues_question(tmp_path: Path) -> None:
    options = args(tmp_path)

    class Backend(Source):
        written: dict[str, Any] | None = None

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            return {
                "board_butler": {
                    "schema_version": 1,
                    "global": {"answer_scope": {"release": "auto"}},
                }
            }

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.written = json.loads(value)

    backend = Backend()
    finding = asyncio.run(
        butler.process_question(backend, question("anything"), options, NOW)
    )

    assert finding["kind"] == "butler_config_invalid"
    assert finding["verdict"] == "ESCALATE"
    assert finding["effective_config"]["effective_mode"] == "shadow"
    assert backend.written is not None


def test_control_action_persists_kill_switch_without_waiting(tmp_path: Path) -> None:
    options = args(tmp_path)
    options.kill_switch = True
    options.control_reason = "operator incident"

    class Backend:
        written: dict[str, Any] | None = None

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.written = json.loads(value)

    backend = Backend()
    asyncio.run(butler.apply_control_action(backend, options, NOW))

    assert backend.written is not None
    assert backend.written["effective_mode"] == "shadow"
    assert backend.written["board_butler"]["kill_switch"] == {
        "engaged": True,
        "reason": "operator incident",
        "at": NOW.isoformat(),
    }


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
                "board_id": "pursers",
                "ticket_id": "TK-source",
                "question_id": f"CQ-{index}",
                "observed_at": NOW.isoformat(),
            }
            for index in range(2)
        ]
    }
    assert (
        butler.rate_limit_reason(state, "pursers", "TK-source", NOW, 5, 2, 20)
        == "per_ticket"
    )
    assert (
        butler.rate_limit_reason(state, "pursers", "TK-other", NOW, 2, 5, 20)
        == "per_hour"
    )
    assert (
        butler.rate_limit_reason(state, "pursers", "TK-other", NOW, 5, 5, 2)
        == "per_board"
    )
    finding = butler.rate_limit_finding(question("anything"), "per_hour", NOW)
    assert finding["kind"] == "butler_queued"
    assert finding["verdict"] == "ESCALATE"
    assert finding["evidence"] == "source=board_butler_rate_limit; limit=per_hour"
    assert "not dropped" in finding["next_action"]


def test_default_rate_limit_reuses_coordinator_intake_shape(tmp_path: Path) -> None:
    options = args(tmp_path)
    config = butler.resolve_config(
        {"intake": {"rate_per_hour": 7}}, options, {"findings": []}, NOW
    )
    assert (config.drafts_per_hour, config.drafts_per_ticket, config.drafts_per_board) == (
        7,
        2,
        20,
    )


@pytest.mark.parametrize(
    "board_butler",
    [
        {"schema_version": 1, "global": {"unexpected": True}},
        {"schema_version": 1, "global": {"required_evidence_kinds": []}},
        {
            "schema_version": 1,
            "global": {"answer_scope": {"gate_waiver": "auto"}},
        },
        {"schema_version": 1, "global": {"allow_self_review": True}},
        {"schema_version": 1, "global": {"allow_merge_main": True}},
        {
            "schema_version": 1,
            "global": {"classification": {"api_key": "forbidden"}},
        },
        {
            "schema_version": 1,
            "global": {
                "active_windows": [
                    {
                        "days": ["wed"],
                        "start": "00:00",
                        "end": "00:00",
                        "timezone": "UTC",
                    }
                ]
            },
        },
        {"global": {}},
    ],
)
def test_declared_config_schema_rejects_unknown_unsafe_or_incomplete_values(
    tmp_path: Path, board_butler: dict[str, Any]
) -> None:
    with pytest.raises(butler.ButlerConfigError):
        butler.resolve_config(
            {"board_butler": board_butler}, args(tmp_path), {"findings": []}, NOW
        )


def test_config_precedence_is_safe_defaults_then_global_project_board(
    tmp_path: Path,
) -> None:
    options = args(tmp_path)
    options.project = "Other"
    document = {
        "board_butler": {
            "schema_version": 1,
            "global": {
                "kill_switch": False,
                "ceilings": {"per_hour": 9, "per_ticket": 8, "per_board": 90},
                "answer_scope": {"ancestry": "auto"},
            },
            "projects": {
                "Pursers": {
                    "ceilings": {"per_hour": 7},
                    "hold_before_post_s": 900,
                },
                "Other": {"ceilings": {"per_hour": 99}},
            },
            "boards": {
                "pursers": {
                    "ceilings": {"per_ticket": 3},
                    "hold_before_post_s": 600,
                }
            },
        }
    }
    config = butler.resolve_config(
        document,
        options,
        {"findings": []},
        NOW,
        project_name="Pursers",
    )

    assert (config.drafts_per_hour, config.drafts_per_ticket, config.drafts_per_board) == (
        7,
        3,
        90,
    )
    assert config.hold_before_post_s == 600
    assert config.answer_scope["ancestry"] == "auto"
    assert config.answer_scope["gate_waiver"] == "escalate"
    assert config.source_layers == (
        "safe_defaults",
        "global",
        "project:Pursers",
        "board:pursers",
    )


def test_project_override_name_is_resolved_from_project_registry() -> None:
    class Client:
        async def board_state_get(self, key: str) -> Mapping[str, Any]:
            assert key == "project_registry"
            return {
                "state": {
                    "value": json.dumps(
                        {
                            "schema_version": 1,
                            "projects": {
                                "Pursers": {
                                    "board_id": "pursers",
                                    "status": "active",
                                },
                                "Other": {"board_id": "other", "status": "active"},
                            },
                        }
                    )
                }
            }

    backend = object.__new__(butler.CentralBackend)
    backend.client = Client()
    backend.args = SimpleNamespace(home_board="pursers")

    assert asyncio.run(backend._project_name_from_registry()) == "Pursers"


def test_active_window_and_task_model_references_are_preserved_exactly(
    tmp_path: Path,
) -> None:
    document = {
        "board_butler": {
            "schema_version": 1,
            "global": {
                "mode": "active",
                "kill_switch": False,
                "answer_scope": {"ticket_status": "auto"},
                "required_evidence_kinds": ["ticket_status"],
                "active_windows": [
                    {
                        "days": ["wed"],
                        "start": "00:00",
                        "end": "23:59",
                        "timezone": "UTC",
                    }
                ],
                "classification": {
                    "model": "Model/Classify-Exact",
                    "endpoint_ref": "endpoint://classification",
                    "key_ref": "secret-ref://classification",
                },
                "drafting": {
                    "model": "Model/Draft-Exact",
                    "endpoint_ref": "endpoint://drafting",
                    "key_ref": "secret-ref://drafting",
                },
            },
        }
    }
    config = butler.resolve_config(document, args(tmp_path), {"findings": []}, NOW)
    reported = config.as_finding()

    assert config.future_active_state == "eligible"
    assert reported["effective_mode"] == "shadow"
    assert reported["classification"] == {
        "model": "Model/Classify-Exact",
        "endpoint_ref": "endpoint://classification",
        "key_ref": "secret-ref://classification",
    }
    assert reported["drafting"]["model"] == "Model/Draft-Exact"


def test_overnight_active_window_uses_previous_day_after_midnight() -> None:
    window = {
        "days": ["wed"],
        "start": "22:00",
        "end": "02:00",
        "timezone": "UTC",
    }

    assert butler._window_allows(
        butler.datetime(2026, 9, 16, 23, 0, tzinfo=butler.timezone.utc), [window]
    )
    assert butler._window_allows(
        butler.datetime(2026, 9, 17, 1, 0, tzinfo=butler.timezone.utc), [window]
    )
    assert not butler._window_allows(
        butler.datetime(2026, 9, 17, 3, 0, tzinfo=butler.timezone.utc), [window]
    )


def test_durable_hold_veto_and_auto_demote_arithmetic(tmp_path: Path) -> None:
    options = args(tmp_path)
    document = {
        "board_butler": {
            "schema_version": 1,
            "global": {
                "mode": "active",
                "kill_switch": False,
                "active_windows": [
                    {
                        "days": ["wed"],
                        "start": "00:00",
                        "end": "23:59",
                        "timezone": "UTC",
                    }
                ],
                "hold_before_post_s": 600,
                "auto_demote": {"veto_count": 2, "window_s": 3600},
            },
        }
    }
    config = butler.resolve_config(document, options, {"findings": []}, NOW)
    first = butler.decorate_finding(
        {
            "kind": "would_answer",
            "question_id": "CQ-1",
            "verdict": "MECHANICAL",
            "answer_class": "ticket_status",
            "evidence_kind": "ticket_status",
        },
        config,
        NOW,
    )
    assert first["hold"]["release_at"] == (NOW + butler.timedelta(seconds=600)).isoformat()
    state = butler.veto_question(
        {"findings": [first]}, "CQ-1", "operator disagreed", NOW
    )
    second = dict(first)
    second["question_id"] = "CQ-2"
    state["findings"].append(second)
    state = butler.veto_question(
        state, "CQ-2", "second operator disagreement", NOW
    )

    restored = json.loads(json.dumps(state))
    assert len(restored["board_butler"]["veto_history"]) == 2
    # The arithmetic survives pruning the larger finding rows and a restart.
    restored["findings"] = []
    demoted = butler.resolve_config(document, options, restored, NOW)
    assert demoted.future_active_state == "auto_demoted"
    assert demoted.demotion_reason == "2 vetoes in 3600 seconds"
    assert state["findings"][0]["hold"]["veto_reason"] == "operator disagreed"


def test_persisted_kill_switch_forces_future_shadow_eligibility_off(
    tmp_path: Path,
) -> None:
    document = {
        "board_butler": {
            "schema_version": 1,
            "global": {"mode": "active", "kill_switch": False},
        }
    }
    state = butler.engage_kill_switch({"findings": []}, "incident", NOW)
    config = butler.resolve_config(document, args(tmp_path), state, NOW)

    assert config.kill_switch is True
    assert config.future_active_state == "killed"
    assert config.demotion_reason == "kill_switch"
    assert state["effective_mode"] == "shadow"


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
