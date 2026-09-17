from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        self.answered: list[dict[str, Any]] = []
        self.identity = SimpleNamespace(
            agent_id="AI-butler",
            agent_name="board-butler-test",
            principal_id="PR-butler",
        )
        self.evaluation_writes = 0
        self.evaluation_values: dict[str, str] = {}

    async def ticket_get(self, ticket_id: str) -> Mapping[str, Any]:
        return {"ticket": self.tickets[ticket_id]}

    async def board_status(self) -> Mapping[str, Any]:
        return {"agents": self.agents}

    async def answered_questions(self) -> list[dict[str, Any]]:
        return self.answered

    async def evaluation(self, question_id: str) -> Mapping[str, Any]:
        value = self.evaluation_values.get(question_id)
        if value is None:
            return {}
        return {"state": {"value": value}}

    async def write_evaluation(
        self, question_id: str, value: str, _expected: str | None
    ) -> None:
        self.evaluation_writes += 1
        self.evaluation_values[question_id] = value


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


def cli_args(tmp_path: Path) -> list[str]:
    return [
        "--token-path",
        str(tmp_path / "token.jwt"),
        "--repo",
        str(REPOSITORY_ROOT),
        "--pid-file",
        str(tmp_path / "butler.pid"),
        "--cursor-file",
        str(tmp_path / "cursor.json"),
    ]


def test_active_mode_requires_separate_private_authorization(
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "active.json"
    base = [
        *cli_args(tmp_path),
        "--runtime-mode",
        "active",
        "--act-on-board",
        "pursers",
    ]
    with pytest.raises(SystemExit):
        butler.parse_args(base)

    authorization.write_text(
        json.dumps({"schema_version": 1, "mode": "active", "authorized": True}),
        encoding="utf-8",
    )
    authorization.chmod(0o600)
    parsed = butler.parse_args(
        [*base, "--active-authorization-file", str(authorization)]
    )

    assert parsed.runtime_mode == "active"
    assert parsed.act_on_board == ["pursers"]

    authorization.chmod(0o644)
    with pytest.raises(SystemExit):
        butler.parse_args(
            [*base, "--active-authorization-file", str(authorization)]
        )


def test_runtime_status_is_private_and_tracks_last_activity(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"

    with butler.RuntimeStatus(path, "shadow") as status:
        started = json.loads(path.read_text(encoding="utf-8"))
        assert started["running"] is True
        assert started["mode"] == "shadow"
        status.mark("question_processed", NOW)
        marked = json.loads(path.read_text(encoding="utf-8"))
        assert marked["last_activity"] == "question_processed"
        assert marked["last_activity_at"] == NOW.isoformat()

    stopped = json.loads(path.read_text(encoding="utf-8"))
    assert stopped["running"] is False
    assert stopped["last_activity"] == "stopped"
    assert path.stat().st_mode & 0o777 == 0o600


def test_singleton_pidfile_is_private(tmp_path: Path) -> None:
    path = tmp_path / "board-butler.pid"

    with butler.SingletonLock(path):
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_local_kill_marker_stops_before_token_or_board_access(tmp_path: Path) -> None:
    options = args(tmp_path)
    marker = tmp_path / "KILLED"
    marker.write_text('{"engaged":true}', encoding="utf-8")
    marker.chmod(0o600)
    options.local_kill_file = marker
    options.token_path.unlink()

    asyncio.run(
        butler.run(
            options,
            backend_factory=lambda *_args: pytest.fail("board access was attempted"),
        )
    )


@contextlib.contextmanager
def provider_server(content: str) -> Any:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            payload = json.dumps({"draft": content}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


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


def test_real_question_precedent_cites_identifiers_without_copying_answer_text() -> None:
    corpus = json.loads(BACKLOG_FIXTURE.read_text(encoding="utf-8"))
    current = corpus["available_records"][1]
    answered = [
        {**row, "state": "answered"} for row in corpus["available_records"]
    ]

    citations = butler.find_precedents(current, answered)

    assert citations
    assert citations[0] == {
        "question_id": "CQ-53524d65cdb51016",
        "ticket_id": "TK-02bf4d01d662",
    }
    encoded = json.dumps(citations)
    assert current["message"] not in encoded
    assert all(row["answer"] not in encoded for row in answered)
    assert all(set(item) == {"question_id", "ticket_id"} for item in citations)


def test_identifier_only_pair_record_never_stores_question_or_answer_text() -> None:
    real = json.loads(BACKLOG_FIXTURE.read_text(encoding="utf-8"))["available_records"][1]
    authored_question = real["message"]
    authored_answer = real["answer"]
    state = butler.record_draft_evaluation(
        {"findings": []},
        {
            "board_id": "pursers",
            "ticket_id": real["ticket_id"],
            "question_id": real["question_id"],
            "kind": real["kind"],
            "message": authored_question,
            "answer": authored_answer,
        },
        {"kind": "would_answer"},
        Source().identity,
        NOW,
    )

    pair = state["evaluation"]
    assert pair["question_id"] == "CQ-7bf548bf5e084198"
    assert pair["ticket_id"] == "TK-02bf4d01d662"
    assert pair["question_kind"] == "decision"
    assert pair["draft_status"] == "declined"
    assert authored_question not in json.dumps(pair)
    assert authored_answer not in json.dumps(pair)
    assert not ({"message", "question", "answer"} & set(pair))


def test_agreement_uses_human_marks_and_suppresses_small_sample_percentage() -> None:
    rows = [
        {"question_kind": "decision", "mark": "send_as_is", "mark_population": "live_answerer", "marked_at": "2026-09-16T10:00:00+00:00"},
        {"question_kind": "decision", "mark": "needed_edits", "mark_population": "live_answerer", "marked_at": "2026-09-16T11:00:00+00:00"},
    ]
    sparse = butler.agreement_by_question_kind(rows)[0]
    assert sparse["sample_count"] == 2
    assert sparse["axis"] == "draft_quality"
    assert sparse["population"] == "live_answerer"
    assert sparse["status"] == "insufficient_samples"
    assert sparse["agreement_percent"] is None

    measured = butler.agreement_by_question_kind(
        [*rows, {"question_kind": "decision", "mark": "send_as_is", "mark_population": "live_answerer", "marked_at": "2026-09-16T12:00:00+00:00"}]
    )[0]
    assert measured["marks"] == {
        "send_as_is": 2,
        "needed_edits": 1,
        "wrong": 0,
    }
    assert measured["agreement_percent"] == 66.7


def test_retrospective_real_marks_stay_separate_and_use_routing_axis() -> None:
    source = json.loads(BACKLOG_FIXTURE.read_text(encoding="utf-8"))
    records = []
    for item in source["available_records"]:
        state = butler.record_draft_evaluation(
            {"schema_version": 1},
            item,
            {
                "kind": "would_answer",
                "verdict": item["expected_verdict"],
                "policy_rule": "historical-replay",
            },
            Source().identity,
            NOW,
        )
        row = state["evaluation"]
        row.update(
            {
                name: item[name]
                for name in (
                    "mark",
                    "mark_population",
                    "marked_by",
                    "marked_at",
                    "mark_source_question_id",
                )
            }
        )
        encoded = json.dumps(row)
        assert item["message"] not in encoded
        assert item["answer"] not in encoded
        records.append(row)

    report = butler.agreement_by_question_kind(records)
    assert report == [
        {
            "question_kind": "decision",
            "axis": "routing_quality",
            "population": "retrospective_operator",
            "sample_count": 3,
            "marks": {
                "correct_escalation": 2,
                "should_have_answered": 0,
                "should_have_escalated": 1,
            },
            "status": "measured",
            "agreement_percent": 66.7,
            "first_marked_at": "2026-09-17T12:08:46.324109+00:00",
            "last_marked_at": "2026-09-17T12:08:46.324109+00:00",
        }
    ]
    ticket_report = butler.agreement_by_ticket(records)
    assert ticket_report[0]["ticket_id"] == "TK-02bf4d01d662"
    assert ticket_report[0]["sample_count"] == 3
    assert ticket_report[0]["agreement_percent"] == 66.7

    repeated = butler.multi_question_tickets(
        source["available_records"] + source["unavailable_records"]
    )
    assert repeated == [
        {"ticket_id": "TK-9ca52e7ad8bf", "question_count": 6},
        {"ticket_id": "TK-84e7e39328f5", "question_count": 4},
        {"ticket_id": "TK-b58fdaba3a63", "question_count": 4},
        {"ticket_id": "TK-02bf4d01d662", "question_count": 3},
        {"ticket_id": "TK-daee8b8ce82f", "question_count": 2},
        {"ticket_id": "TK-db6ca1290d33", "question_count": 2},
    ]


def test_production_backfill_persists_real_marks_without_authored_text() -> None:
    corpus = json.loads(BACKLOG_FIXTURE.read_text(encoding="utf-8"))

    class Backend:
        identity = Source().identity
        values: dict[str, str] = {}
        writes = 0

        async def evaluation(self, question_id: str) -> Mapping[str, Any]:
            value = self.values.get(question_id)
            return {"state": {"value": value}} if value is not None else {}

        async def write_evaluation(
            self, question_id: str, value: str, expected: str | None
        ) -> None:
            current = self.values.get(question_id)
            assert current == expected
            self.values[question_id] = value
            self.writes += 1

    backend = Backend()
    first = asyncio.run(
        butler.backfill_retrospective_evaluations(
            backend, NOW, board_id="pursers"
        )
    )
    second = asyncio.run(
        butler.backfill_retrospective_evaluations(
            backend, NOW, board_id="pursers"
        )
    )

    assert first == {"created": 3, "updated": 0, "unchanged": 0}
    assert second == {"created": 0, "updated": 0, "unchanged": 3}
    assert backend.writes == 3
    documents = [json.loads(value) for value in backend.values.values()]
    rows = [document["evaluation"] for document in documents]
    assert len(rows) == 3
    assert {row["question_id"] for row in rows} == {
        "CQ-53524d65cdb51016",
        "CQ-7bf548bf5e084198",
        "CQ-08843e9944e1cf22",
    }
    encoded = json.dumps(documents)
    assert all(item["message"] not in encoded for item in corpus["available_records"])
    assert all(item["answer"] not in encoded for item in corpus["available_records"])
    report = butler.agreement_by_question_kind(rows)
    assert report[0]["sample_count"] == 3
    assert report[0]["agreement_percent"] == 66.7

    other_board = asyncio.run(
        butler.backfill_retrospective_evaluations(
            backend, NOW, board_id="another-board"
        )
    )
    assert other_board == {"created": 0, "updated": 0, "unchanged": 0}
    assert backend.writes == 3


def test_production_backfill_rejects_conflicting_existing_mark() -> None:
    specification = butler.RETROSPECTIVE_EVALUATION_BACKFILL[0]
    document = butler.retrospective_evaluation_document(
        specification, Source().identity, NOW
    )
    document["evaluation"]["mark"] = "wrong"

    with pytest.raises(ValueError, match="conflicts with an existing mark"):
        butler._merge_retrospective_evaluation(
            document,
            butler.retrospective_evaluation_document(
                specification, Source().identity, NOW
            ),
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
    backend.evaluation_values["CQ-source"] = json.dumps(
        {
            "schema_version": 1,
            "evaluation": {
                "question_id": "CQ-source",
                "ticket_id": "TK-source",
                "question_kind": "information",
                "draft_status": "produced",
            },
        }
    )
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


def test_process_question_uses_reloaded_provider_without_exposing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = args(tmp_path)
    secret_root = tmp_path / "provider-secrets"
    secret_root.mkdir()
    secret = "resident-cycle-secret-6471"
    key_file = secret_root / "butler.key"
    key_file.write_text(secret, encoding="utf-8")
    key_file.chmod(0o600)
    options.provider_secrets_dir = secret_root

    class Backend(Source):
        endpoint = ""
        model = ""
        writes: list[dict[str, Any]] = []
        project_name = "Pursers"

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            provider = {
                "model": self.model,
                "endpoint_ref": self.endpoint,
                "key_ref": "file:butler.key",
                "extra_headers": {"X-Butler-Test": "cycle"},
                "draft_path": "generate",
                "draft_protocol": "pursers_json_v1",
            }
            return {
                "board_butler": {
                    "schema_version": 1,
                    "global": {
                        "classification": dict(provider),
                        "drafting": dict(provider),
                    },
                }
            }

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.writes.append(json.loads(value))

    backend = Backend()
    with provider_server("first provider draft") as (first_url, first_requests):
        backend.endpoint = first_url
        backend.model = "model-first"
        first = asyncio.run(
            butler.process_question(
                backend,
                {**question("Anything?"), "question_id": "CQ-first"},
                options,
                NOW,
            )
        )
    with provider_server("second provider draft") as (second_url, second_requests):
        backend.endpoint = second_url
        backend.model = "model-second"
        second = asyncio.run(
            butler.process_question(
                backend,
                {**question("Anything else?"), "question_id": "CQ-second"},
                options,
                NOW + butler.timedelta(seconds=1),
            )
        )
    with provider_server(secret) as (unsafe_url, unsafe_requests):
        backend.endpoint = unsafe_url
        backend.model = "model-unsafe"
        unsafe = asyncio.run(
            butler.process_question(
                backend,
                {**question("Unsafe echo?"), "question_id": "CQ-unsafe"},
                options,
                NOW + butler.timedelta(seconds=2),
            )
        )

    assert first["message"] == "first provider draft"
    assert second["message"] == "second provider draft"
    assert len(first_requests) == len(second_requests) == 1
    assert first_requests[0]["path"] == second_requests[0]["path"] == "/v1/generate"
    assert first_requests[0]["body"]["model"] == "model-first"
    assert second_requests[0]["body"]["model"] == "model-second"
    assert first_requests[0]["body"]["protocol"] == "pursers_json_v1"
    assert first_requests[0]["body"]["input"]["question"] == "Anything?"
    assert len(unsafe_requests) == 1
    assert unsafe_requests[0]["body"]["model"] == "model-unsafe"
    assert first_requests[0]["headers"]["Authorization"] == f"Bearer {secret}"
    assert second_requests[0]["headers"]["X-Butler-Test"] == "cycle"
    assert secret not in json.dumps(first_requests[0]["body"])
    assert secret not in json.dumps(backend.writes)
    assert secret not in json.dumps(first)
    assert secret not in json.dumps(second)
    assert unsafe["message"] == (
        "Would escalate because the configured drafting provider failed."
    )
    assert unsafe["draft_source"] == "configured_provider_failed"
    assert secret not in json.dumps(unsafe)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_process_question_rejects_noncanonical_key_before_provider_or_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = args(tmp_path)
    secret_root = tmp_path / "provider-secrets"
    secret_root.mkdir()
    secret = "resident-cycle-secret-6471 "
    key_file = secret_root / "butler.key"
    key_file.write_text(secret, encoding="utf-8")
    key_file.chmod(0o600)
    options.provider_secrets_dir = secret_root

    class Backend(Source):
        written: dict[str, Any] | None = None
        project_name = "Pursers"

        async def findings(self) -> Mapping[str, Any]:
            return {}

        async def coordinator_config(self) -> Mapping[str, Any]:
            provider = {
                "model": "model-unsafe",
                "endpoint_ref": "http://127.0.0.1:9/v1",
                "key_ref": "file:butler.key",
            }
            return {
                "board_butler": {
                    "schema_version": 1,
                    "global": {
                        "classification": dict(provider),
                        "drafting": dict(provider),
                    },
                }
            }

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.written = json.loads(value)

    backend = Backend()
    finding = asyncio.run(
        butler.process_question(
            backend,
            {**question("Unsafe key?"), "question_id": "CQ-unsafe-key"},
            options,
            NOW,
        )
    )

    assert finding["kind"] == "butler_config_invalid"
    assert finding["verdict"] == "ESCALATE"
    assert secret not in json.dumps(finding)
    assert backend.written is not None
    assert secret not in json.dumps(backend.written)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


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


@pytest.mark.parametrize("control", ["kill-switch", "veto-question"])
def test_control_command_runs_while_resident_lock_is_held(
    tmp_path: Path, control: str
) -> None:
    options = args(tmp_path)
    options.control_reason = "operator incident"
    initial: dict[str, Any] = {"findings": []}
    if control == "kill-switch":
        options.kill_switch = True
    else:
        options.veto_question = "CQ-held"
        initial["findings"] = [
            {
                "kind": "would_answer",
                "question_id": "CQ-held",
                "hold": {"status": "held"},
            }
        ]

    class Backend:
        written: dict[str, Any] | None = None

        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> "Backend":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def findings(self) -> Mapping[str, Any]:
            return {"state": {"value": json.dumps(initial)}}

        async def write_findings(self, value: str, _expected: str | None) -> None:
            self.written = json.loads(value)

    backend = Backend()
    with butler.SingletonLock(options.pid_file):
        asyncio.run(butler.run(options, backend_factory=lambda *_args: backend))

    assert backend.written is not None
    assert backend.written["effective_mode"] == "shadow"
    if control == "kill-switch":
        assert backend.written["board_butler"]["kill_switch"]["engaged"] is True
    else:
        assert backend.written["board_butler"]["last_veto"] == {
            "question_id": "CQ-held",
            "reason": "operator incident",
            "at": backend.written["generated_at"],
        }
        assert backend.written["findings"][0]["hold"]["status"] == "vetoed"


def test_cli_resident_lock_failure_exits_nonzero(tmp_path: Path) -> None:
    options = args(tmp_path)
    argv = [
        "--token-path",
        str(options.token_path),
        "--repo",
        str(options.repo),
        "--pid-file",
        str(options.pid_file),
        "--cursor-file",
        str(options.cursor_file),
        "--once",
    ]

    with butler.SingletonLock(options.pid_file):
        with pytest.raises(SystemExit) as raised:
            butler.main(argv)

    assert raised.value.code == 1


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
        "extra_headers": {},
        "key_header": "Authorization",
        "key_prefix": "Bearer",
        "validation_path": "models",
        "draft_path": "draft",
        "draft_protocol": "pursers_json_v1",
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


def test_module_has_no_question_answer_claim_submit_or_assign_path() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "ticket_question_" + "answer",
        "ticket_" + "submit",
        "ticket_" + "claim",
        "ticket_" + "assign",
    )
    assert all(name not in source for name in forbidden)
    assert "ticket_update(action.ticket_id, parked=True)" in source
    assert source.count("ticket_annotate(") == 2
    assert "board_catchup" not in source
    assert "ticket_list" not in source


def test_mechanical_plan_parks_only_after_threshold_without_live_worker() -> None:
    snapshot = {
        "agents": [
            {
                "agent_id": "AI-viewer",
                "agent_name": "fleet-dashboard-viewer",
                "role": "worker",
                "lifecycle_status": "active",
                "last_activity_at": NOW.isoformat(),
                "capabilities_explicit": True,
                "capabilities": {"can_work": False},
            }
        ]
    }
    ticket = {
        "ticket_id": "TK-loop",
        "status": "open",
        "parked": False,
        "dispatch_history": [
            {
                "state": "broadcast",
                "kind": "work",
                "reason": "no_live_candidates",
                "cycle": cycle,
            }
            for cycle in range(3)
        ],
        "annotations": [],
    }

    actions = butler.plan_mechanical_actions(
        "fullplatts",
        snapshot,
        {"findings": []},
        {"TK-loop": ticket},
        NOW,
        no_live_candidates_cycles=3,
    )

    assert [(action.kind, action.ticket_id, action.observed_cycles) for action in actions] == [
        ("park_no_live_candidates", "TK-loop", 3)
    ]
    snapshot["agents"].append(
        {
            "agent_id": "AI-worker",
            "agent_name": "worker-1",
            "role": "worker",
            "lifecycle_status": "active",
            "last_activity_at": NOW.isoformat(),
            "capabilities_explicit": True,
            "capabilities": {"can_work": True},
        }
    )
    assert butler.plan_mechanical_actions(
        "fullplatts",
        snapshot,
        {"findings": []},
        {"TK-loop": ticket},
        NOW,
        no_live_candidates_cycles=3,
    ) == []


def test_mechanical_plan_refuses_incapable_target_and_names_identity() -> None:
    snapshot = {
        "agents": [
            {
                "agent_id": "AI-viewer",
                "agent_name": "fleet-dashboard-viewer",
                "role": "worker",
                "lifecycle_status": "active",
                "last_activity_at": NOW.isoformat(),
                "capabilities_explicit": True,
                "capabilities": {"can_work": False},
            }
        ]
    }
    actions = butler.plan_mechanical_actions(
        "fullplatts",
        snapshot,
        {
            "findings": [
                {
                    "kind": "starved",
                    "ticket_id": "TK-loop",
                    "would_assign_to_agent_id": "AI-viewer",
                    "would_assign_to_agent_name": "fleet-dashboard-viewer",
                }
            ]
        },
        {"TK-loop": {"status": "open", "annotations": []}},
        NOW,
        no_live_candidates_cycles=3,
    )

    assert len(actions) == 1
    assert actions[0].kind == "refuse_incapable_target"
    assert actions[0].identity_name == "fleet-dashboard-viewer"
    assert actions[0].reason == "capabilities.can_work is not true"


def test_mechanical_action_registers_durable_vetoable_hold() -> None:
    action = butler.MechanicalAction(
        "park_no_live_candidates",
        "fullplatts",
        "TK-loop",
        None,
        None,
        26,
        "repeated no_live_candidates cycles and no live can_work=true seat",
    )
    finding = butler.mechanical_action_finding(action, NOW, 60)

    assert finding["kind"] == "would_answer"
    assert finding["action_class"] == "park_no_live_candidates"
    assert finding["question_id"].startswith("BA-")
    assert finding["hold"] == {
        "status": "pending",
        "drafted_at": NOW.isoformat(),
        "release_at": (NOW + butler.timedelta(seconds=60)).isoformat(),
        "vetoable_until": (NOW + butler.timedelta(seconds=60)).isoformat(),
        "veto_reason": None,
    }
    state = butler.veto_question(
        {"findings": [finding]}, finding["question_id"], "operator veto", NOW
    )
    assert state["findings"][0]["hold"]["status"] == "vetoed"
    assert butler.mechanical_hold_status(finding, NOW) == "held"
    assert butler.mechanical_hold_status(
        finding, NOW + butler.timedelta(seconds=60)
    ) == "ready"
    assert butler.mechanical_hold_status(state["findings"][0], NOW) == "vetoed"


def test_mechanical_action_id_is_stable_across_reoffer_counts() -> None:
    first = butler.MechanicalAction(
        "park_no_live_candidates", "fullplatts", "TK-loop", None, None, 3, "reason"
    )
    later = butler.MechanicalAction(
        "park_no_live_candidates", "fullplatts", "TK-loop", None, None, 26, "reason"
    )

    assert butler.mechanical_action_id(first) == butler.mechanical_action_id(later)


@pytest.mark.parametrize(
    "action",
    [
        butler.MechanicalAction(
            "park_no_live_candidates",
            "fullplatts",
            "TK-loop",
            None,
            None,
            3,
            "repeated no_live_candidates cycles and no live can_work=true seat",
        ),
        butler.MechanicalAction(
            "refuse_incapable_target",
            "fullplatts",
            "TK-loop",
            "fleet-dashboard-viewer",
            "AI-viewer",
            None,
            "capabilities.can_work is not true",
        ),
    ],
    ids=["park_no_live_candidates", "refuse_incapable_target"],
)
def test_mechanical_hold_predicate_disappears_then_gets_fresh_window(
    action: butler.MechanicalAction,
) -> None:
    first_state, first_status = butler.ensure_mechanical_hold({}, action, NOW, 60)
    action_id = butler.mechanical_action_id(action)

    assert first_status == "registered"
    withdrawn_state, withdrawn = butler.reconcile_mechanical_holds(
        first_state, set(), NOW + butler.timedelta(seconds=10)
    )
    old_finding = withdrawn_state["findings"][0]
    assert withdrawn == [action_id]
    assert old_finding["hold"]["status"] == "withdrawn"
    assert old_finding["hold"]["withdrawn_at"] == (
        NOW + butler.timedelta(seconds=10)
    ).isoformat()

    reappeared_at = NOW + butler.timedelta(seconds=120)
    renewed_state, renewed_status = butler.ensure_mechanical_hold(
        withdrawn_state, action, reappeared_at, 60
    )
    renewed_finding = renewed_state["findings"][0]
    assert renewed_status == "registered"
    assert renewed_finding["hold"]["status"] == "pending"
    assert renewed_finding["hold"]["drafted_at"] == reappeared_at.isoformat()
    assert renewed_finding["hold"]["release_at"] == (
        reappeared_at + butler.timedelta(seconds=60)
    ).isoformat()
    assert butler.mechanical_hold_status(renewed_finding, reappeared_at) == "held"


def test_mechanical_hold_reconciliation_preserves_veto_fail_closed() -> None:
    action = butler.MechanicalAction(
        "park_no_live_candidates", "fullplatts", "TK-loop", None, None, 3, "reason"
    )
    state, _status = butler.ensure_mechanical_hold({}, action, NOW, 60)
    vetoed = butler.veto_question(
        state, butler.mechanical_action_id(action), "operator veto", NOW
    )

    reconciled, withdrawn = butler.reconcile_mechanical_holds(
        vetoed, set(), NOW + butler.timedelta(seconds=10)
    )
    reappeared, status = butler.ensure_mechanical_hold(
        reconciled, action, NOW + butler.timedelta(seconds=120), 60
    )

    assert withdrawn == []
    assert status == "vetoed"
    assert reappeared["findings"][0]["hold"]["status"] == "vetoed"


def test_registry_refresh_runs_real_derivation_for_two_active_boards_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = args(tmp_path, dry_run=True)
    options.refresh_seconds = 60
    options.act_on_board = []
    options.no_live_candidates_cycles = 3
    backend = butler.CentralBackend(options, "opaque")
    calls: list[argparse.Namespace] = []

    class Reader:
        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> "Reader":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    projects = [
        SimpleNamespace(board_id="pursers"),
        SimpleNamespace(board_id="fullplatts"),
    ]

    async def read_cycle(_reader: Reader, _home: str) -> tuple[Any, Any, Any]:
        return projects, {"pursers": {}, "fullplatts": {}}, {
            "pursers": {}, "fullplatts": {}
        }

    def parse_args(_argv: list[str]) -> argparse.Namespace:
        return argparse.Namespace()

    async def run(parsed: argparse.Namespace) -> None:
        calls.append(parsed)

    monkeypatch.setattr(
        backend,
        "_coordinator_api",
        lambda: {
            "RawReader": Reader,
            "read_cycle": read_cycle,
            "parse_args": parse_args,
            "run": run,
        },
    )
    first = asyncio.run(backend.refresh_registry_findings(NOW))
    second = asyncio.run(
        backend.refresh_registry_findings(NOW + butler.timedelta(seconds=60))
    )

    assert first["active_boards"] == second["active_boards"] == [
        "fullplatts", "pursers"
    ]
    assert first["refreshed_at"] != second["refreshed_at"]
    assert len(calls) == 2
