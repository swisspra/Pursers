from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1] / "technical_recovery.py"
spec = importlib.util.spec_from_file_location("technical_recovery", MODULE)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
sys.modules.setdefault(spec.name, recovery)
spec.loader.exec_module(recovery)
questions = recovery._TECHNICAL_QUESTIONS_MODULE

BOARD = "board-demo"
PROJECT = "Demo"
QUESTION = {
    "question_id": "CQ-demo-1", "project": PROJECT,
    "message": "Is FE03 independently accepted, or is its current SHA only a candidate?",
    "kind": "information", "state": "open", "asked_at": "2026-10-26T00:00:00+00:00",
    "asked_by": {"agent_id": "worker-1", "agent_name": "worker", "principal_id": "PR-worker"},
    "in_reply_to": None,
}


def registry_for(*names, op=True, batch=1):
    projects = {}
    for name in names:
        operations = ["technical_hold_resume", "dispatch_reconcile"]
        if op:
            operations.append("technical_question_answer")
        projects[name] = {
            "board_id": BOARD, "status": "active", "fleet": True,
            "work_dir": "/fixture/" + name,
            "butler_recovery_policy": {
                "schema_version": 1, "mode": "active", "activation_id": "operator:fixture",
                "project_id": name, "board_id": BOARD, "operations": operations,
                "sweep_interval_s": 300, "project_timeout_s": 30,
                "max_projects_per_sweep": batch, "page_size": 25,
                "max_no_progress_attempts": 3, "retry_backoff_s": 300,
            },
        }
    return {"schema_version": 1, "projects": projects}


def ticket(ticket_id="TK-question", project=PROJECT, question=None):
    return {
        "ticket_id": ticket_id, "target_url": project + "/src", "title": "Scoped FE03 test",
        "description": "Implement the scoped ticket and submit the exact candidate.",
        "scope": "interactive", "required_fields": [], "forbidden": [],
        "related_files": [], "status": "claimed", "dispatch_state": {"state": "claimed"},
        "claimed_by_agent_id": "worker-1", "claimed_by_principal_id": "PR-worker",
        "lease_expires_at": "2026-10-26T01:00:00+00:00", "review_lease": None,
        "latest_submission": None, "latest_verdict": None,
        "coordinator_questions": [copy.deepcopy(question or QUESTION)], "annotations": [],
    }


class Client:
    agent_name = "fixture-butler"
    identity = {"principal_id": "PR-butler", "role": "coordinator"}

    def __init__(self, registry, tickets=()):
        self.state = {"project_registry": json.dumps(registry)}
        self.tickets = {row["ticket_id"]: copy.deepcopy(row) for row in tickets}
        self.inbox_calls = 0
        self.ticket_list_calls = 0
        self.ticket_get_calls = []
        self.answer_calls = []
        self.fail_after_answer = False

    async def board_state_get(self, key):
        if key not in self.state:
            raise ValueError("state key not found")
        return {"state": {"value": self.state[key]}}

    async def _call(self, tool, args):
        assert tool == "board_state_update"
        key, value = args["key"], args["value"]
        old = self.state.get(key)
        if args.get("expected_absent") and old is not None:
            raise ValueError("state precondition failed")
        if args.get("expected_sha256") and (
            old is None or hashlib.sha256(old.encode()).hexdigest() != args["expected_sha256"]
        ):
            raise ValueError("state precondition failed")
        self.state[key] = value
        return {"ok": True}

    async def ticket_list(self, **kwargs):
        self.ticket_list_calls += 1
        rows = list(self.tickets.values())
        return {"tickets": copy.deepcopy(rows), "total_matching": len(rows), "next_cursor": None}

    async def board_question_inbox(self, **kwargs):
        self.inbox_calls += 1
        rows = []
        for row in self.tickets.values():
            for item in row.get("coordinator_questions", []):
                if item.get("state") == kwargs.get("state", "open"):
                    rows.append({**copy.deepcopy(item), "ticket_id": row["ticket_id"]})
        return {"questions": rows[:kwargs.get("limit", 100)], "total": len(rows)}

    async def ticket_get(self, ticket_id, **kwargs):
        return {"ticket": copy.deepcopy(self.tickets[ticket_id])}

    async def ticket_question_answer(self, ticket_id, question_id, *, action, message):
        self.answer_calls.append((ticket_id, question_id, action, message))
        selected = next(q for q in self.tickets[ticket_id]["coordinator_questions"]
                        if q["question_id"] == question_id)
        selected.update(state="answered", answer=message,
                        answered_by={"principal_id": "PR-butler", "agent_id": "butler-1"})
        if self.fail_after_answer:
            self.fail_after_answer = False
            raise TimeoutError("answer committed but response lost")
        return {"question": copy.deepcopy(selected)}


class Backend:
    def __init__(self, client, *, runtime_mode="active"):
        self.client = client
        self.args = SimpleNamespace(runtime_mode=runtime_mode, act_on_registry=True,
                                    act_on_board=[], home_board=BOARD, dry_run=False,
                                    kill_switch=False, veto_question=False)
        self.model_calls = []
        self.recommendation = {"decision": "resume", "category": "scoped_submission",
                               "reason": "The question concerns current scoped evidence.",
                               "note": "FE03 is currently a candidate only; no matching independent acceptance is recorded. Continue owned scoped checks, then submit the exact candidate for independent review; final combined validation remains required."}

    @asynccontextmanager
    async def _client_for_board(self, board):
        assert board == BOARD
        yield self.client

    async def _technical_recovery_authorized(self, context):
        return True

    async def _technical_recovery_choose(self, context):
        self.model_calls.append(copy.deepcopy(context))
        return dict(self.recommendation)


def run_tick(backend, clock=lambda: 1000.0):
    return asyncio.run(recovery.TechnicalRecoverySupervisor(backend, clock=clock).tick())


def test_supervisor_answers_exact_question_with_real_route_and_preserves_work_lease():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        before = copy.deepcopy(client.tickets["TK-question"])
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        after = client.tickets["TK-question"]
        assert len(backend.model_calls) == 1
        assert backend.model_calls[0]["operation"] == "technical_question_answer"
        assert backend.model_calls[0]["question"]["question_id"] == QUESTION["question_id"]
        assert "same-project" in backend.model_calls[0]["selection"]
        assert after["coordinator_questions"][0]["state"] == "answered"
        assert after["coordinator_questions"][0]["answered_by"]["principal_id"] == "PR-butler"
        assert after["status"] == before["status"]
        assert after["claimed_by_principal_id"] == before["claimed_by_principal_id"]
        assert after["lease_expires_at"] == before["lease_expires_at"]
        assert len(client.answer_calls) == 1
    asyncio.run(scenario())


def test_terminal_ticket_questions_never_read_inbox_or_call_provider():
    async def scenario():
        for status in ("closed", "canceled", "cancelled", "terminated"):
            row = ticket(ticket_id="TK-terminal-" + status)
            row["status"] = status
            client = Client(registry_for(PROJECT), [row])
            backend = Backend(client)
            await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
            assert client.inbox_calls == 0
            assert backend.model_calls == []
            assert client.answer_calls == []
    asyncio.run(scenario())


def test_closed_same_project_ticket_remains_read_only_accepted_source_reference():
    current = ticket()
    accepted = ticket("TK-accepted-source")
    accepted.update(status="closed", latest_submission={
        "notes": "branch_and_commit: pursers/TK-accepted-source @ " + "a" * 40,
        "submitted_by_principal_id": "PR-worker",
    }, latest_verdict={"verdict": "approve", "submission_commit": "a" * 40,
                       "status_to": "closed", "submitted_by_principal_id": "PR-worker",
                       "reviewed_by_principal_id": "PR-reviewer"})
    registry = registry_for(PROJECT)
    policy = recovery.parse_recovery_policy(registry["projects"][PROJECT]["butler_recovery_policy"],
                                            project_id=PROJECT, board_id=BOARD, active=True)
    context = questions.build_context(registry=registry, policy=policy, question=QUESTION,
                                      ticket=current, rows=[current, accepted])
    assert context["related_ticket_summaries"] == [{
        "ticket_id": "TK-accepted-source", "candidate_sha": "a" * 40,
        "accepted_sha": "a" * 40, "review_state": "closed", "review_verdict": "approve",
        "detail_ref": {"tool": "ticket_get", "ticket_id": "TK-accepted-source", "view": "full"},
    }]


def test_resident_nonterminal_page_fetches_closed_foundation_and_downstream_harness():
    async def scenario():
        current = ticket()
        foundation, harness = ticket("TK-foundation"), ticket("TK-harness")
        for row in (foundation, harness):
            row.update(status="closed", latest_submission={
                "candidate_sha": "a" * 40, "submitted_by_principal_id": "PR-worker"},
                latest_verdict={"verdict": "approve", "submission_commit": "a" * 40,
                    "status_to": "closed", "submitted_by_principal_id": "PR-worker",
                    "reviewed_by_principal_id": "PR-reviewer"})
        registry = registry_for(PROJECT)
        registry["projects"][PROJECT]["butler_workflow"] = {
            "schema_version": 1, "workflow_id": "test-flow", "project_id": PROJECT,
            "board_id": BOARD, "cohort_id": "test-cohort", "sealed": True,
            "nodes": [
                {"node_id": "foundation", "ticket_id": foundation["ticket_id"],
                 "requires": [], "acceptance_checks": ["Scoped checks"]},
                {"node_id": "current", "ticket_id": current["ticket_id"],
                 "requires": ["foundation"], "acceptance_checks": ["Scoped checks"]},
                {"node_id": "harness", "ticket_id": harness["ticket_id"],
                 "requires": ["current"], "acceptance_checks": ["Scoped checks"]}],
            "final_validation": {"title": "Final QA", "acceptance_checks": ["Combined QA"]},
        }
        # Normalized registry nodes are the actual resident producer shape.
        from pursers_client.project_workflow import parse_project_workflow
        workflow = registry["projects"][PROJECT]["butler_workflow"]
        registry["projects"][PROJECT]["butler_workflow"] = parse_project_workflow(
            workflow, project_id=PROJECT, board_id=BOARD)
        client = Client(registry, [current, foundation, harness])
        backend = Backend(client)
        supervisor = recovery.TechnicalRecoverySupervisor(backend)
        policy = supervisor._policy(registry, PROJECT)
        candidates = await supervisor.question_answerer.candidates(client, registry, policy, [current])
        assert len(candidates) == 1
        references = candidates[0]["context"]["related_ticket_summaries"]
        assert {r["ticket_id"] for r in references} == {"TK-foundation", "TK-harness"}
        assert all(r["accepted_sha"] == "a" * 40 for r in references)
        # Both model paths use exactly the same bounded source references.
        enriched = await supervisor._context_evidence(client, {"ticket": current}, registry, policy, current)
        assert enriched["related_ticket_summaries"] == references
        assert len(json.dumps(candidates[0]["context"]).encode()) < 18_000
    asyncio.run(scenario())


def test_question_fresh_fence_rejects_workspace_progress_changed_during_provider():
    async def scenario():
        current = ticket()
        client = Client(registry_for(PROJECT), [current])
        backend = Backend(client)
        facts = {"workspace_status": "verified", "workspace_source_progress": "before"}
        async def read(registry, context):
            assert context["ticket"]["description"] == current["description"]
            return dict(facts)
        original = backend._technical_recovery_choose
        async def choose(context):
            result = await original(context)
            facts["workspace_source_progress"] = "after"
            return result
        backend._technical_recovery_workspace_evidence = read
        backend._technical_recovery_choose = choose
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert len(backend.model_calls) == 1
        assert not client.answer_calls
        assert client.tickets[current["ticket_id"]]["coordinator_questions"][0]["state"] == "open"
    asyncio.run(scenario())


def test_resident_hydrates_oversized_work_stub_before_routing_and_question_selection():
    async def scenario():
        current = ticket()
        class StubClient(Client):
            async def ticket_list(self, **kwargs):
                assert kwargs["view"] == "work"
                return {"tickets": [{"ticket_id": current["ticket_id"], "status": "claimed",
                    "oversized_for_list": True, "detail_ref": "board://fixture/ticket/one"}],
                    "total_matching": 1, "next_cursor": None}
        client = StubClient(registry_for(PROJECT), [current])
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert len(backend.model_calls) == 1
        assert len(client.answer_calls) == 1
        assert client.tickets[current["ticket_id"]]["coordinator_questions"][0]["state"] == "answered"
    asyncio.run(scenario())


def test_unrelated_related_ticket_status_and_lease_churn_is_not_decision_evidence():
    current = ticket()
    related = ticket("TK-related")
    registry = registry_for(PROJECT)
    policy = recovery.parse_recovery_policy(registry["projects"][PROJECT]["butler_recovery_policy"],
                                            project_id=PROJECT, board_id=BOARD, active=True)
    context = questions.build_context(registry=registry, policy=policy, question=QUESTION,
                                      ticket=current, rows=[current, related])
    related.update(status="claimed", claimed_by_agent_id="another-worker",
                   lease_expires_at="later", dispatch_state={"state": "claimed"})
    after = questions.build_context(registry=registry, policy=policy, question=QUESTION,
                                    ticket=current, rows=[current, related])
    assert context["related_ticket_summaries"] == after["related_ticket_summaries"]
    assert questions._progress_identity(context) == questions._progress_identity(after)


def test_closed_ticket_reconciles_an_already_applying_answer_receipt_read_only():
    async def scenario():
        row = ticket("TK-closed-receipt")
        row["status"] = "closed"
        row["coordinator_questions"][0].update(
            state="answered", answer="Persisted exact answer",
            answered_by={"principal_id": "PR-butler", "agent_id": "butler-1"})
        client = Client(registry_for(PROJECT), [row])
        backend = Backend(client)
        policy = recovery.parse_recovery_policy(json.loads(client.state["project_registry"])["projects"][PROJECT]["butler_recovery_policy"],
                                                project_id=PROJECT, board_id=BOARD, active=True)
        key = questions._question_key(policy, row["ticket_id"], QUESTION["question_id"])
        await questions._write_state(client, key, None, {
            "schema_version": 1, "status": "applying", "proposal": {
                "decision": "resume", "category": "scoped_submission",
                "reason": "Previously authorized exact answer.", "note": "Persisted exact answer"},
            "input_digest": "prior-digest", "ticket_id": row["ticket_id"],
            "question_id": QUESTION["question_id"],
        })
        engine = recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0)
        engine.question_answerer.supervisor = engine
        candidates = await engine.question_answerer.candidates(
            client, json.loads(client.state["project_registry"]), policy, [row])
        assert len(candidates) == 1 and candidates[0].get("receipt_only") is True
        result = await engine.question_answerer.answer(candidates[0])
        assert result["status"] == "completed"
        assert client.inbox_calls == 0
        assert backend.model_calls == []
        assert client.answer_calls == []
        assert json.loads(client.state[key])["status"] == "completed"
    asyncio.run(scenario())


def test_legacy_policy_does_not_read_or_answer_questions():
    async def scenario():
        client = Client(registry_for(PROJECT, op=False), [ticket()])
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert client.inbox_calls == 0
        assert backend.model_calls == []
        assert client.answer_calls == []
    asyncio.run(scenario())


def test_same_question_restart_and_heartbeat_do_not_pay_or_answer_twice():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1301.0).tick()
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1602.0).tick()
        assert len(backend.model_calls) == 1
        assert len(client.answer_calls) == 1
    asyncio.run(scenario())


def test_lost_answer_response_reconciles_exact_own_receipt_without_duplicate():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        client.fail_after_answer = True
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        assert len(backend.model_calls) == 1
        assert len(client.answer_calls) == 1
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1301.0).tick()
        assert len(backend.model_calls) == 1
        assert len(client.answer_calls) == 1
        records = [json.loads(value) for key, value in client.state.items()
                   if key.startswith("technical_question_answer_")]
        assert records[0]["status"] == "completed"
    asyncio.run(scenario())


def test_question_context_marks_unaccepted_candidate_and_rechecks_changed_facts():
    async def scenario():
        current = ticket()
        current["latest_submission"] = {
            "notes": "branch_and_commit: pursers/TK-source @ " + "a" * 40,
            "submitted_by_principal_id": "PR-worker",
        }
        current["latest_verdict"] = {"verdict": "approve", "submission_commit": "b" * 40,
                                  "status_to": "closed", "submitted_by_principal_id": "PR-worker",
                                  "reviewed_by_principal_id": "PR-reviewer"}
        client = Client(registry_for(PROJECT), [current])
        backend = Backend(client)
        async def choose(context):
            backend.model_calls.append(copy.deepcopy(context))
            row = client.tickets["TK-question"]
            row["status"] = "closed"
            row["latest_verdict"]["submission_commit"] = "a" * 40
            row["latest_submission"]["submission_preflight"] = {"commit": "a" * 40}
            return dict(backend.recommendation)
        backend._technical_recovery_choose = choose
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        assert backend.model_calls[0]["ticket"]["source_status"]["candidate_sha"] == "a" * 40
        assert backend.model_calls[0]["ticket"]["source_status"]["accepted_sha"] is None
        assert not client.answer_calls
    asyncio.run(scenario())


def test_due_scheduler_registers_all_projects_before_batch_selection():
    async def scenario():
        client = Client(registry_for("Alpha", "Beta", batch=1), [])
        backend = Backend(client)
        clock = [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(backend, clock=lambda: clock[0])
        await engine.tick()
        assert engine.scheduler.cursor == "Alpha"
        assert engine.next_due == {"Alpha": 1300.0, "Beta": 1000.0}
        assert engine.next_delay() == 0.1
        clock[0] += engine.next_delay()
        await engine.tick()
        assert engine.scheduler.cursor == "Beta"
        assert client.ticket_list_calls == 2
        assert client.inbox_calls == 0
        assert backend.model_calls == []
    asyncio.run(scenario())


def test_two_project_routes_are_isolated_and_progress_independently():
    async def scenario():
        registry = registry_for("Alpha", "Beta", batch=1)
        alpha, beta = ticket("TK-alpha", "Alpha"), ticket("TK-beta", "Beta")
        alpha["coordinator_questions"][0]["project"] = "Alpha"
        beta["coordinator_questions"][0]["project"] = "Beta"
        client = Client(registry, [alpha, beta])
        backend = Backend(client)
        clock = [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(backend, clock=lambda: clock[0])
        await engine.tick()
        assert [call["project_id"] for call in backend.model_calls] == ["Alpha"]
        assert client.tickets["TK-alpha"]["coordinator_questions"][0]["state"] == "answered"
        assert client.tickets["TK-beta"]["coordinator_questions"][0]["state"] == "open"
        clock[0] += engine.next_delay()
        await engine.tick()
        assert [call["project_id"] for call in backend.model_calls] == ["Alpha", "Beta"]
        assert client.tickets["TK-beta"]["coordinator_questions"][0]["state"] == "answered"
    asyncio.run(scenario())


def test_non_home_board_question_uses_registered_route_without_home_board_listing():
    async def scenario():
        other_board = "board-project"
        client = Client(registry_for(PROJECT), [ticket()])
        client.state["project_registry"] = json.dumps({
            "schema_version": 1,
            "projects": {PROJECT: {
                **registry_for(PROJECT)["projects"][PROJECT],
                "board_id": other_board,
                "butler_recovery_policy": {
                    **registry_for(PROJECT)["projects"][PROJECT]["butler_recovery_policy"],
                    "board_id": other_board,
                },
            }},
        })
        backend = Backend(client)
        # Registry authority lives on the home board; ticket reads are routed
        # to the registered project board and must never list the home board.
        home_client = Client(json.loads(client.state["project_registry"]))
        clients = {BOARD: home_client, other_board: client}

        @asynccontextmanager
        async def client_for_board(board):
            yield clients[board]

        backend._client_for_board = client_for_board
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert clients[BOARD].ticket_list_calls == 0
        assert clients[other_board].ticket_list_calls == 1
        assert backend.model_calls[0]["board_id"] == other_board
        assert clients[other_board].tickets["TK-question"]["coordinator_questions"][0]["state"] == "answered"
    asyncio.run(scenario())


def test_worker_lease_renewal_does_not_repurchase_an_escalated_answer():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        backend.recommendation = {
            "decision": "escalate", "category": "human",
            "reason": "External registered contract is unknown.",
            "note": "Ask the domain owner; do not invent registration facts.",
        }
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        client.tickets["TK-question"]["lease_expires_at"] = "2026-10-26T02:00:00+00:00"
        client.tickets["TK-question"]["dispatch_state"]["heartbeat_at"] = "2026-10-26T01:30:00+00:00"
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1301.0).tick()
        assert len(backend.model_calls) == 1
        assert client.answer_calls == []
    asyncio.run(scenario())


def test_persisted_decision_restarts_without_rebuying_or_losing_the_answer():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        original = client._call
        interrupted = False

        async def interrupt_after_decision(tool, arguments):
            nonlocal interrupted
            result = await original(tool, arguments)
            document = json.loads(arguments["value"])
            if document.get("status") == "decided" and not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            return result

        client._call = interrupt_after_decision
        with pytest.raises(asyncio.CancelledError):
            await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        assert len(backend.model_calls) == 1 and client.answer_calls == []
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1301.0).tick()
        assert len(backend.model_calls) == 1
        assert len(client.answer_calls) == 1
    asyncio.run(scenario())


def test_repeated_question_ids_and_model_changes_cannot_reset_progress_budget():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        version = 0

        async def contract(context):
            return {"model": f"fixture-model-{version}"}

        backend._technical_recovery_provider_contract = contract
        for version in range(4):
            question = {**copy.deepcopy(QUESTION), "question_id": f"CQ-repeat-{version}",
                        "asked_at": f"2026-10-26T00:0{version}:00+00:00"}
            client.tickets["TK-question"]["coordinator_questions"] = [question]
            await recovery.TechnicalRecoverySupervisor(
                backend, clock=lambda: 1000.0 + version * 301).tick()
        assert len(backend.model_calls) == 3
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "open"
        client.tickets["TK-question"]["latest_submission"] = {"commit_hash": "a" * 40}
        version = 4
        question = {**copy.deepcopy(QUESTION), "question_id": "CQ-after-source-change"}
        client.tickets["TK-question"]["coordinator_questions"] = [question]
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 2300.0).tick()
        assert len(backend.model_calls) == 4
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "answered"
    asyncio.run(scenario())


def test_foreign_question_owner_and_unknown_sso_escalation_are_never_auto_answered():
    async def scenario():
        foreign = copy.deepcopy(QUESTION)
        foreign["question_id"] = "CQ-foreign"
        foreign["accepted_by"] = {"agent_id": "another-coordinator", "principal_id": "PR-other"}
        current = ticket(question=foreign)
        client = Client(registry_for(PROJECT), [current])
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert backend.model_calls == []
        assert client.answer_calls == []

        unknown = copy.deepcopy(QUESTION)
        unknown["question_id"] = "CQ-unknown-sso"
        unknown["message"] = "Confirm whether production SSO registration exists for this tenant."
        current = ticket(ticket_id="TK-unknown", question=unknown)
        client = Client(registry_for(PROJECT), [current])
        backend = Backend(client)
        backend.recommendation = {"decision": "escalate", "category": "human",
                                  "reason": "The external SSO registration fact is not in current project evidence.",
                                  "note": "A project owner must confirm the tenant's production SSO registration."}
        await recovery.TechnicalRecoverySupervisor(backend).tick()
        assert len(backend.model_calls) == 1
        assert not client.answer_calls
        assert current["coordinator_questions"][0]["state"] == "open"
    asyncio.run(scenario())


def test_policy_kill_switch_and_concurrent_question_fences_preserve_current_question():
    async def scenario():
        for change in ("policy", "kill_switch", "concurrent_question"):
            current = ticket()
            client = Client(registry_for(PROJECT), [current])
            backend = Backend(client)
            original_choose = backend._technical_recovery_choose

            async def choose(context, change=change):
                if change == "kill_switch":
                    backend.args.kill_switch = True
                result = await original_choose(context)
                if change == "policy":
                    changed = registry_for(PROJECT)
                    changed["projects"][PROJECT]["butler_recovery_policy"]["sweep_interval_s"] = 301
                    client.state["project_registry"] = json.dumps(changed)
                elif change == "kill_switch":
                    pass
                else:
                    new_question = copy.deepcopy(QUESTION)
                    new_question["question_id"] = "CQ-arrived-during-call"
                    client.tickets["TK-question"]["coordinator_questions"].append(new_question)
                return result

            backend._technical_recovery_choose = choose
            await recovery.TechnicalRecoverySupervisor(backend).tick()
            questions = client.tickets["TK-question"]["coordinator_questions"]
            if change == "concurrent_question":
                assert questions[0]["state"] == "answered"
                assert questions[1]["state"] == "open"
                assert len(client.answer_calls) == 1
            else:
                assert questions[0]["state"] == "open"
                assert client.answer_calls == []
    asyncio.run(scenario())


def test_mixed_human_holds_and_question_jobs_are_fair_under_shared_cap():
    async def scenario():
        rows = [ticket(f"TK-human-{index}") for index in range(5)]
        for row in rows:
            row["status"] = "needs_human"
            row["coordinator_questions"] = []
        question_job = ticket("TK-question-job")
        question_job["coordinator_questions"][0]["project"] = PROJECT
        rows.append(question_job)
        client = Client(registry_for(PROJECT), rows)
        backend = Backend(client)
        clock = [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(backend, clock=lambda: clock[0])
        processed = []

        async def fake_recover(_client, _registry, _policy, ticket_id):
            processed.append(ticket_id)
            return {"ticket_id": ticket_id, "status": "simulated"}

        engine._recover = fake_recover
        await engine.tick()
        assert len(processed) == 4
        assert backend.model_calls == []
        clock[0] += 300.0
        await engine.tick()
        assert "TK-question-job" in [call["ticket"]["ticket_id"] for call in backend.model_calls]
        assert client.tickets["TK-question-job"]["coordinator_questions"][0]["state"] == "answered"
    asyncio.run(scenario())


def test_provider_failure_backoff_is_durable_and_limited():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        backend._technical_recovery_choose = lambda context: (_ for _ in ()).throw(TimeoutError())
        clock = [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(backend, clock=lambda: clock[0])
        await engine.tick()
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: clock[0]).tick()
        assert client.inbox_calls == 2
        records = [json.loads(value) for key, value in client.state.items()
                   if key.startswith("technical_question_answer_")]
        assert records[0]["status"] == "provider_failed"
        assert records[0]["attempts"] == 1
        assert not client.answer_calls
    asyncio.run(scenario())


def test_detail_overflow_returns_detail_required_without_paid_call():
    async def scenario():
        current = ticket()
        current["description"] = "x" * 19_000
        client = Client(registry_for(PROJECT), [current])
        backend = Backend(client)
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        assert not backend.model_calls
        assert not client.answer_calls
        state = [json.loads(value) for key, value in client.state.items()
                 if key.startswith("technical_recovery_supervision_")][0]
        assert state["actions"][0]["status"] == "detail_required"
    asyncio.run(scenario())


def test_unchanged_superseded_proposal_replays_through_fences_without_paying_again():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        original_choose = backend._technical_recovery_choose

        async def supersede_each_attempt(context):
            result = await original_choose(context)
            # Cause only the post-provider safety fence to reject this attempt;
            # restore the kill switch before the next sweep so input is identical.
            backend.args.kill_switch = True
            return result

        backend._technical_recovery_choose = supersede_each_attempt
        for attempt in range(5):
            await recovery.TechnicalRecoverySupervisor(
                backend, clock=lambda: 1000.0 + attempt * 301).tick()
            backend.args.kill_switch = False
        assert len(backend.model_calls) == 1
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "answered"
        assert [json.loads(value)["attempts"] for key, value in client.state.items()
                if key.startswith("technical_question_budget_")] == [1]
    asyncio.run(scenario())


def test_recent_scoped_fact_is_kept_even_when_lexically_smaller_than_old_facts():
    current = ticket()
    current["annotations"] = [{"kind":"evidence", "text":f"Z older fact {i}"} for i in range(4)]
    current["annotations"].append({"kind":"evidence", "text":"A new accepted source fact"})
    projection = questions._ticket_context(current)
    assert {"kind":"evidence", "text":"A new accepted source fact"} in projection["latest_scoped_evidence"]


def test_registered_prerequisite_is_prioritized_ahead_of_unrelated_same_project_history():
    registry = registry_for(PROJECT)
    policy = recovery.parse_recovery_policy(registry["projects"][PROJECT]["butler_recovery_policy"],
                                            project_id=PROJECT, board_id=BOARD, active=True)
    current = ticket()
    rows = [ticket(f"TK-a{i}") for i in range(12)] + [ticket("TK-z-required")]
    refs = questions._related_summaries(rows, current, registry, policy, ["TK-z-required"])
    assert len(refs) == 8
    assert "TK-z-required" in {row["ticket_id"] for row in refs}
    assert refs == questions._related_summaries(list(reversed(rows)), current, registry, policy, ["TK-z-required"])


def test_same_question_provider_retries_with_changed_contract_share_budget():
    async def scenario():
        client = Client(registry_for(PROJECT), [ticket()])
        backend = Backend(client)
        version = [0]
        async def contract(context):
            return {"model": version[0]}

        backend._technical_recovery_provider_contract = contract
        calls = []

        async def fail(context):
            calls.append(copy.deepcopy(context))
            raise TimeoutError("fixture provider failure")

        backend._technical_recovery_choose = fail
        for attempt in range(5):
            version[0] = attempt
            await recovery.TechnicalRecoverySupervisor(
                backend, clock=lambda: 1000.0 + attempt * 301).tick()
        assert len(calls) == 3
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "open"
        assert [json.loads(value)["attempts"] for key, value in client.state.items()
                if key.startswith("technical_question_budget_")] == [3]
    asyncio.run(scenario())


def test_equivalent_new_questions_ignore_annotation_timestamps_and_related_row_order():
    async def scenario():
        registry = registry_for(PROJECT)
        registry["projects"][PROJECT]["butler_recovery_policy"]["max_no_progress_attempts"] = 2
        current = ticket()
        current["latest_submission"] = {
            "notes": "branch_and_commit: pursers/TK-question @ " + "a" * 40,
            "submitted_by_principal_id": "PR-worker", "submitted_at": "initial-time",
        }
        current["latest_verdict"] = {
            "verdict": "reject", "submission_commit": "a" * 40,
            "status_to": "claimed", "submitted_by_principal_id": "PR-worker",
            "reviewed_by_principal_id": "PR-reviewer", "review_notes": "Same review fact.",
            "reviewed_at": "initial-review-time",
        }
        related_a, related_b = ticket("TK-related-a"), ticket("TK-related-b")
        related_a["coordinator_questions"] = []
        related_b["coordinator_questions"] = []
        client = Client(registry, [current, related_a, related_b])
        backend = Backend(client)
        backend.recommendation = {
            "decision": "escalate", "category": "human",
            "reason": "No source or review facts changed.",
            "note": "Keep the exact question open for an authorized human decision.",
        }
        for attempt in range(4):
            row = client.tickets["TK-question"]
            row["coordinator_questions"] = [{
                **copy.deepcopy(QUESTION), "question_id": f"CQ-equivalent-{attempt}",
                "asked_at": f"2026-10-26T00:0{attempt}:00+00:00",
            }]
            row["annotations"] = [{"kind": "evidence", "text": "same source fact",
                                   "at": f"2026-10-26T00:0{attempt}:30+00:00"}]
            row["dispatch_state"]["heartbeat_at"] = f"heartbeat-{attempt}"
            row["lease_expires_at"] = f"lease-{attempt}"
            related_a["dispatch_state"]["heartbeat_at"] = f"related-heartbeat-{attempt}"
            row["latest_submission"]["submitted_at"] = f"submission-time-{attempt}"
            row["latest_submission"]["runtime_model_usage"] = {"updated_at": attempt}
            row["latest_verdict"]["reviewed_at"] = f"review-time-{attempt}"
            if attempt % 2:
                client.tickets = {"TK-question": row, "TK-related-b": related_b,
                                  "TK-related-a": related_a}
            else:
                client.tickets = {"TK-question": row, "TK-related-a": related_a,
                                  "TK-related-b": related_b}
            await recovery.TechnicalRecoverySupervisor(
                backend, clock=lambda: 1000.0 + attempt * 301).tick()
        assert len(backend.model_calls) == 2
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "open"
        assert [json.loads(value)["attempts"] for key, value in client.state.items()
                if key.startswith("technical_question_budget_")] == [2]
    asyncio.run(scenario())


def test_review_fact_change_allows_only_bounded_new_question_decision():
    async def scenario():
        registry = registry_for(PROJECT)
        registry["projects"][PROJECT]["butler_recovery_policy"]["max_no_progress_attempts"] = 1
        current = ticket()
        current["latest_submission"] = {
            "notes": "branch_and_commit: pursers/TK-question @ " + "a" * 40,
            "submitted_by_principal_id": "PR-worker",
        }
        current["latest_verdict"] = {"verdict": "reject", "submission_commit": "a" * 40,
                                  "status_to": "claimed", "submitted_by_principal_id": "PR-worker",
                                  "reviewed_by_principal_id": "PR-reviewer", "reason": "Fix scoped issue."}
        client = Client(registry, [current])
        backend = Backend(client)
        backend.recommendation = {
            "decision": "escalate", "category": "human",
            "reason": "Review decision is unchanged.",
            "note": "Ask for an authorized decision on the unchanged review finding.",
        }
        for attempt in range(2):
            client.tickets["TK-question"]["coordinator_questions"] = [{
                **copy.deepcopy(QUESTION), "question_id": f"CQ-review-{attempt}"}]
            await recovery.TechnicalRecoverySupervisor(
                backend, clock=lambda: 1000.0 + attempt * 301).tick()
        assert len(backend.model_calls) == 1
        client.tickets["TK-question"]["latest_verdict"]["reason"] = "A new independent review finding."
        client.tickets["TK-question"]["coordinator_questions"] = [{
            **copy.deepcopy(QUESTION), "question_id": "CQ-review-changed"}]
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1700.0).tick()
        assert len(backend.model_calls) == 2
        assert client.tickets["TK-question"]["coordinator_questions"][0]["state"] == "open"
    asyncio.run(scenario())


def test_real_central_resident_tick_answers_question_without_changing_worker_lease(tmp_path, monkeypatch):
    import pursers_central.central as central

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys":[]}')
    for key, value in {
        "CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp", "CENTRAL_JWKS_PATH": str(jwks),
        "CENTRAL_ADMISSION": "invite", "STORE_BACKEND": "sqlite",
    }.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server("localhost", 8765, tmp_path / "central")
    admin = central.Principal("PR-admin", "admin", frozenset({"board:read", "board:write", "board:coordinate"}))
    butler = central.Principal("PR-butler", "butler", frozenset({"board:read", "board:write", "board:coordinate"}))
    worker = central.Principal("PR-worker", "worker", frozenset({"board:read", "board:write"}))
    actor = [admin]
    monkeypatch.setattr(central, "current_principal", lambda: actor[0])
    monkeypatch.setattr(central, "get_access_token", lambda: SimpleNamespace(token="disposable-test-only"))

    async def call(who, tool, **args):
        actor[0] = who
        result = await mcp.call_tool(tool, {"board_id": BOARD, **args})
        assert not result.is_error, (tool, result.content)
        return result.structured_content

    class CentralClient:
        agent_name = "fixture-butler"
        identity = SimpleNamespace(principal_id="PR-butler", role="coordinator", agent_id="")

        async def _call(self, tool, args):
            return await call(butler, tool, **args)

        async def board_state_get(self, key):
            return await call(butler, "board_state_get", key=key)

        async def ticket_list(self, **kwargs):
            return await call(butler, "ticket_list", agent_name=self.agent_name, **kwargs)

        async def ticket_get(self, ticket_id, **kwargs):
            return await call(butler, "ticket_get", ticket_id=ticket_id, **kwargs)

        async def board_question_inbox(self, **kwargs):
            return await call(butler, "board_question_inbox", agent_name=self.agent_name, **kwargs)

        async def ticket_question_answer(self, ticket_id, question_id, *, action, message):
            return await call(butler, "ticket_question_answer", agent_name=self.agent_name,
                              ticket_id=ticket_id, question_id=question_id, action=action,
                              message=message,
                              host_binding=hashlib.sha256(json.dumps(
                                  ["disposable-test-only", self.identity.agent_id],
                                  separators=(",", ":")).encode()).hexdigest())

    async def scenario():
        await call(admin, "board_join", agent_name="admin")
        for who in (butler, worker):
            service.mutate(BOARD, lambda doc, who=who: doc["principal_memberships"].update({
                who.principal_id: {"principal_id": who.principal_id, "role": "member",
                                   "source": "fixture", "created_at": "2026-10-26T00:00:00+00:00"}}))
        joined = await call(butler, "board_join", agent_name="fixture-butler", role="coordinator",
                            capabilities={"can_work": False, "can_review": False})
        await call(worker, "board_join", agent_name="fixture-worker", role="worker",
                   capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        CentralClient.identity.agent_id = joined["agent_id"]
        registry = registry_for(PROJECT)
        await call(admin, "board_state_update", agent_name="admin", key="project_registry",
                   value=json.dumps(registry))
        await call(admin, "board_state_update", agent_name="admin", key="project_coordinators",
                   value=json.dumps({PROJECT: [CentralClient.identity.agent_id]}))
        await call(admin, "ticket_create", agent_name="admin", ticket_id="TK-real-cq",
                   title="Exact current question", description="Scoped work remains held by worker.",
                   target_url=PROJECT + "/src", project=PROJECT, scope="interactive",
                   required_fields=["description", "tests"], tier=1, assigned_to="fixture-worker")
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id="TK-real-cq")
        asked = await call(worker, "ticket_question_ask", agent_name="fixture-worker",
                           ticket_id="TK-real-cq", message=QUESTION["message"], kind="information")
        backend = Backend(CentralClient())
        await recovery.TechnicalRecoverySupervisor(backend, clock=lambda: 1000.0).tick()
        result = await call(butler, "ticket_get", ticket_id="TK-real-cq", view="full")
        final = result["ticket"]
        question = next(q for q in final["coordinator_questions"] if q["question_id"] == asked["question_id"])
        assert question["state"] == "answered"
        assert question["answered_by"]["principal_id"] == "PR-butler"
        assert final["status"] == "claimed"
        assert final["claimed_by_principal_id"] == "PR-worker"
        assert backend.model_calls[0]["operation"] == "technical_question_answer"
    asyncio.run(scenario())
