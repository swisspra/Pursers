"""Keep full Central state local and meter every paid prompt boundary."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from test_source_intake import butler, NOW


def runtime(protocol="openai_chat_completions_v1"):
    return butler.ProviderRuntime(endpoint="https://provider.example.invalid/v1", model="test-model",
                                  credential="fixture-only", draft_protocol=protocol)


@pytest.mark.parametrize("protocol", ["openai_chat_completions_v1", "pursers_json_v1"])
def test_question_prompt_rejects_oversized_evidence_before_provider_io(monkeypatch, protocol):
    calls = []
    async def post(*args, **kwargs):
        calls.append(args)
        return {"draft": "answer", "choices": [{"message": {"content": "answer"}}]}
    monkeypatch.setattr(butler, "_post_provider_json", post)
    with pytest.raises(ValueError, match="prompt exceeded the safe bound"):
        asyncio.run(butler.draft_with_provider(runtime(protocol), {"message": "Check status"},
            {"evidence": "literal evidence " * butler.MAX_PROVIDER_PROMPT_CHARS}))
    assert calls == []


@pytest.mark.parametrize("protocol", ["openai_chat_completions_v1", "pursers_json_v1"])
def test_question_prompt_excludes_unrelated_ticket_and_board_payload(protocol):
    raw = "UNRELATED_ARCHIVED_HISTORY_" * 10000
    question = {"message": "สถานะงานนี้เป็นอย่างไร", "kind": "information", "tickets": [raw], "snapshot": raw}
    finding = {"verdict": "MECHANICAL", "policy_rule": "ticket-status", "evidence": "status=closed",
               "message": "The ticket is closed.", "ticket": {"submission_history": [raw]}, "board": raw}
    result = json.loads(butler._provider_request_body(runtime(protocol), question, finding))
    inputs = result["input"] if protocol == "pursers_json_v1" else json.loads(result["messages"][1]["content"])
    assert inputs == {"question": question["message"], "question_kind": "information", "verdict": "MECHANICAL",
                      "policy_rule": "ticket-status", "evidence": "status=closed", "fallback_draft": finding["message"]}
    assert raw not in json.dumps(result)
    if protocol == "openai_chat_completions_v1":
        assert question["message"] in result["messages"][1]["content"]


def test_intake_prompt_uses_counts_independent_of_ticket_history(monkeypatch):
    bodies = []
    async def post(_runtime, body, **kwargs):
        bodies.append(json.loads(body))
        return {"choices": [{"message": {"content": '{"pull":1,"source_ids":["sonar"],"reason":"capacity"}'}}]}
    monkeypatch.setattr(butler, "_post_provider_json", post)
    def snapshot(multiplier):
        return {"coordination_tickets_complete": True, "coordination_tickets": [
            {"ticket_id": "TK-one", "status": "open", "description": "FULL_TICKET_" * multiplier,
             "annotations": [{"text": "HISTORY_" * multiplier}]}], "agents": []}
    for size in (1, 100000):
        context = {"ceiling": 2, "in_flight_by_source": {},
                   "sources": [{"source_id": "sonar", "open_issue_count": 163}],
                   "board_load": {"work": butler.source_intake_board_load(snapshot(size), NOW)},
                   "fleet_load": butler.source_intake_fleet_load({"work": snapshot(size)}, NOW)}
        asyncio.run(butler.decide_intake_with_provider(runtime(), context))
    assert bodies[0] == bodies[1]
    prompt = bodies[0]["messages"][1]["content"]
    assert "FULL_TICKET_" not in prompt and "HISTORY_" not in prompt
    assert json.loads(prompt)["board_load"]["work"]["open"] == 1


def test_intake_unicode_remains_readable_in_the_model_prompt(monkeypatch):
    async def post(_runtime, body, **kwargs):
        prompt = json.loads(body)["messages"][1]["content"]
        assert "โครงการทดสอบ" in prompt
        return {"choices": [{"message": {"content": '{"pull":0,"source_ids":[],"reason":"wait"}'}}]}
    monkeypatch.setattr(butler, "_post_provider_json", post)
    asyncio.run(butler.decide_intake_with_provider(runtime(), {"board_load": {"โครงการทดสอบ": {"open": 1}}}))


@pytest.mark.parametrize("protocol", ["openai_chat_completions_v1", "pursers_json_v1"])
def test_maximum_central_question_still_fits_without_truncation(protocol):
    # Central accepts 2,000 characters per question; Thai uses three UTF-8 bytes.
    message = "ก" * 2000
    result = json.loads(butler._provider_request_body(runtime(protocol), {"message": message},
        {"evidence": "status=closed", "message": "The ticket is closed."}))
    inputs = result["input"] if protocol == "pursers_json_v1" else json.loads(result["messages"][1]["content"])
    assert inputs["question"] == message


@pytest.mark.parametrize("protocol", ["openai_chat_completions_v1", "pursers_json_v1"])
def test_question_budget_counts_utf8_bytes_without_truncating_evidence(protocol):
    with pytest.raises(ValueError, match="prompt exceeded the safe bound"):
        butler._provider_request_body(runtime(protocol), {"message": "Check status"}, {"evidence": "ก" * 4100})


def test_group_prompt_preserves_unicode_and_rejects_oversize_before_provider_io(monkeypatch):
    calls = []
    async def config(): return {}
    monkeypatch.setattr(butler, "resolve_config", lambda *a, **kw: None)
    monkeypatch.setattr(butler, "resolve_provider_runtime", lambda *a, **kw: runtime())
    async def post(_runtime, body, **kwargs):
        calls.append(json.loads(body))
        return {"choices": [{"message": {"content": '{"groups":[]}'}}]}
    monkeypatch.setattr(butler, "_post_provider_json", post)
    backend = SimpleNamespace(coordinator_config=config, args=SimpleNamespace(), project_name="test")
    asyncio.run(butler.CentralBackend._source_group_choose(backend,
        {"issue_count": 1, "candidates": [{"message": "แก้ข้อความซ้ำ", "occurrences": [{"index": 0, "line": 7}]}]}))
    assert "แก้ข้อความซ้ำ" in calls[0]["messages"][1]["content"]
    with pytest.raises(ValueError, match="group planning context exceeded bound"):
        asyncio.run(butler.CentralBackend._source_group_choose(backend, {"candidates": ["x" * 40000]}))
    assert len(calls) == 1
