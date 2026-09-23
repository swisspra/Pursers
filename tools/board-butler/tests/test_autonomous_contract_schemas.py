from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "docs" / "design" / "schemas"
SHA = "0" * 64


def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def validate(name: str, value: dict) -> None:
    jsonschema.Draft202012Validator(
        load(name), format_checker=jsonschema.FormatChecker()
    ).validate(value)


def config() -> dict:
    count = {"min": 0, "target": 1, "max": 2}
    budget = {
        "period": "hour",
        "max_tokens": 1000,
        "max_cost_microunits": 1000,
        "max_external_calls": 10,
    }
    return {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": "pursers",
        "revision": 1,
        "enabled": True,
        "host_runtime": {
            "host_ref": "host:one",
            "revision": 1,
            "agent_process_ceiling": 12,
            "control_plane_processes": 2,
            "total_process_ceiling": 14,
            "configured_by": "operator:one",
            "configured_at": "2026-09-23T00:00:00Z",
        },
        "desired": {
            "mode": "autonomous",
            "runner": "direct_api",
            "capacity": {
                "worker": count,
                "reviewer": count,
                "acp_worker": count,
            },
            "host_concurrency": 3,
            "board_concurrency": 3,
            "cooldowns": {
                "scale_up_s": 5,
                "scale_down_s": 5,
                "failure_backoff_s": 5,
            },
            "budget": budget,
            "connectors": [
                {
                    "connector_id": "connector:one",
                    "enabled": True,
                    "transport": "streamable_http",
                    "protocol_revision": "2026-07-28",
                    "endpoint_ref": "endpoint:one",
                    "secret_ref": "secret:one",
                    "tools": [
                        {
                            "name": "lookup",
                            "effect": "read_only",
                            "replay": "safe_with_stable_call_id",
                            "stable_call_id_field": "call_id",
                        },
                        {
                            "name": "mutate",
                            "effect": "mutating",
                            "replay": "never",
                            "stable_call_id_field": None,
                        },
                    ],
                    "resources": [],
                    "limits": {
                        "timeout_ms": 1000,
                        "max_input_bytes": 1000,
                        "max_output_bytes": 1000,
                        "max_concurrency": 1,
                        "calls_per_minute": 10,
                    },
                }
            ],
        },
        "envelope": {
            "fingerprint_sha256": SHA,
            "approved_template_ids": ["template:worker"],
            "approved_connector_ids": ["connector:one"],
            "max_capacity": {"worker": 2, "reviewer": 2, "acp_worker": 2},
            "max_host_concurrency": 3,
            "max_board_concurrency": 3,
            "max_budget": budget,
            "created_by": "operator:one",
            "created_at": "2026-09-23T00:00:00Z",
        },
        "authorization": {
            "authorization_id": "authorization:one",
            "config_revision": 1,
            "envelope_fingerprint_sha256": SHA,
            "expires_at": "2026-09-24T00:00:00Z",
        },
    }


def model_request() -> dict:
    return {
        "schema": "autonomous_butler_model_v1",
        "schema_version": 1,
        "message_type": "request",
        "request_id": "request:one",
        "board_id": "pursers",
        "subject_id": "question:one",
        "task_kind": "answer_question",
        "policy_digest_sha256": SHA,
        "task_input": {
            "instruction": "Answer the eligible information question.",
            "observation_refs": ["observation:one"],
            "constraints": ["Return only the task-schema object."],
        },
        "task_input_digest_sha256": SHA,
        "task_schema": {"schema_id": "answer:v1", "schema_sha256": SHA},
        "evidence_refs": ["evidence:one"],
        "max_output_bytes": 4096,
        "usage_limit": {
            "max_input_tokens": 1000,
            "max_output_tokens": 200,
            "max_cost_microunits": 1000,
        },
        "deadline": "2026-09-23T00:01:00Z",
        "cancellation_token": "cancel:one",
    }


def model_result() -> dict:
    return {
        "schema": "autonomous_butler_model_v1",
        "schema_version": 1,
        "message_type": "result",
        "request_id": "request:one",
        "board_id": "pursers",
        "policy_digest_sha256": SHA,
        "outcome": "succeeded",
        "proposal_json": '{"answer":"bounded proposal"}',
        "citations": ["evidence:one"],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_microunits": 5,
            "measured": True,
        },
        "completed_at": "2026-09-23T00:00:30Z",
        "error": None,
    }


def executor_request() -> dict:
    return {
        "schema": "autonomous_butler_executor_v1",
        "schema_version": 1,
        "message_type": "request",
        "operation_id": "operation:one",
        "board_id": "pursers",
        "action": "inspect",
        "seat_id": "seat:one",
        "template_id": "template:worker",
        "template_digest_sha256": SHA,
        "expected_seat_generation": 1,
        "authorization_fingerprint_sha256": SHA,
        "deadline": "2026-09-23T00:01:00Z",
        "caller_auth": {
            "scheme": "local_ed25519_v1",
            "key_id": "executor-key:one",
            "nonce": "nonce:one",
            "signed_at": "2026-09-23T00:00:00Z",
            "request_digest_sha256": SHA,
            "signature_base64": "A" * 86 + "==",
        },
    }


def command() -> dict:
    return {
        "schema": "autonomous_butler_command_v1",
        "schema_version": 1,
        "command_id": "command:one",
        "board_id": "pursers",
        "kind": "reconcile_now",
        "actor_id": "operator:one",
        "created_at": "2026-09-23T00:00:00Z",
        "expected_config_revision": 1,
        "payload": {},
        "state": "accepted",
        "revision": 1,
        "transition": {
            "prior_revision": 0,
            "current_revision": 1,
            "actor_id": "operator:one",
            "reason_code": "command_accepted",
            "audit_id": "audit:one",
            "occurred_at": "2026-09-23T00:00:00Z",
        },
    }


def test_all_contract_schemas_are_strict_draft_2020_12() -> None:
    paths = sorted(SCHEMA_DIR.glob("autonomous-butler-*.schema.json"))
    assert {
        "autonomous-butler-config-v1.schema.json",
        "autonomous-butler-command-v1.schema.json",
        "autonomous-butler-command-v2.schema.json",
        "autonomous-butler-state-v1.schema.json",
        "autonomous-butler-executor-v1.schema.json",
        "autonomous-butler-audit-v1.schema.json",
        "autonomous-butler-control-audit-v2.schema.json",
        "autonomous-butler-model-v1.schema.json",
    } <= {path.name for path in paths}
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


def test_explicit_enable_and_authorization_are_required_for_autonomous_mode() -> None:
    valid = config()
    validate("autonomous-butler-config-v1.schema.json", valid)

    missing = copy.deepcopy(valid)
    del missing["enabled"]
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-config-v1.schema.json", missing)

    disabled = copy.deepcopy(valid)
    disabled["enabled"] = False
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-config-v1.schema.json", disabled)

    unauthorized = copy.deepcopy(valid)
    del unauthorized["authorization"]
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-config-v1.schema.json", unauthorized)


def test_connector_replay_metadata_requires_stable_call_id_semantics() -> None:
    valid = config()
    tool = valid["desired"]["connectors"][0]["tools"][0]
    tool["stable_call_id_field"] = None
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-config-v1.schema.json", valid)

    valid = config()
    tool = valid["desired"]["connectors"][0]["tools"][1]
    tool["stable_call_id_field"] = "call_id"
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-config-v1.schema.json", valid)


def test_common_model_contract_rejects_unknown_fields_and_unmeasured_usage() -> None:
    validate("autonomous-butler-model-v1.schema.json", model_request())
    validate("autonomous-butler-model-v1.schema.json", model_result())

    unknown = model_request()
    unknown["provider_api_key"] = "forbidden"
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-model-v1.schema.json", unknown)

    unmeasured = model_result()
    unmeasured["usage"]["measured"] = False
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-model-v1.schema.json", unmeasured)

    missing_usage = model_result()
    del missing_usage["usage"]
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-model-v1.schema.json", missing_usage)


def test_model_failure_requires_null_proposal_and_reason() -> None:
    failed = model_result()
    failed.update(
        outcome="failed",
        proposal_json=None,
        reason_code="provider_failed",
        error={"category": "provider", "code": "provider_failed", "retryable": True},
    )
    validate("autonomous-butler-model-v1.schema.json", failed)

    missing_reason = copy.deepcopy(failed)
    del missing_reason["reason_code"]
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-model-v1.schema.json", missing_reason)

    cancelled = copy.deepcopy(failed)
    cancelled.update(outcome="cancelled", reason_code="cancelled")
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-model-v1.schema.json", cancelled)


def test_executor_request_requires_complete_caller_authentication() -> None:
    valid = executor_request()
    validate("autonomous-butler-executor-v1.schema.json", valid)

    for field in ("caller_auth",):
        invalid = copy.deepcopy(valid)
        del invalid[field]
        with pytest.raises(jsonschema.ValidationError):
            validate("autonomous-butler-executor-v1.schema.json", invalid)
    for field in ("key_id", "nonce", "request_digest_sha256", "signature_base64"):
        invalid = copy.deepcopy(valid)
        del invalid["caller_auth"][field]
        with pytest.raises(jsonschema.ValidationError):
            validate("autonomous-butler-executor-v1.schema.json", invalid)


def test_command_requires_complete_typed_transition_record() -> None:
    valid = command()
    validate("autonomous-butler-command-v1.schema.json", valid)

    missing = copy.deepcopy(valid)
    del missing["transition"]
    with pytest.raises(jsonschema.ValidationError):
        validate("autonomous-butler-command-v1.schema.json", missing)

    for field in ("prior_revision", "current_revision", "actor_id", "reason_code", "audit_id"):
        invalid = copy.deepcopy(valid)
        del invalid["transition"][field]
        with pytest.raises(jsonschema.ValidationError):
            validate("autonomous-butler-command-v1.schema.json", invalid)
