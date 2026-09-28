from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


MODULE = Path(__file__).resolve().parents[1] / "supervisor_roster.py"
SPEC = importlib.util.spec_from_file_location("supervisor_roster", MODULE)
assert SPEC and SPEC.loader
roster = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = roster
SPEC.loader.exec_module(roster)

FIXTURE = Path(__file__).with_name("fixtures") / "supervisor_demand_shift.json"
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def grant(cap: int = 4, *, cooldown: int = 120) -> Any:
    return roster.SupervisorGrant(
        board_id="pursers",
        config_revision=7,
        envelope_fingerprint_sha256=FINGERPRINT,
        host_seat_cap=cap,
        gate_concurrency_ceiling=3,
        approved_template_ids=(
            "template:worker:direct",
            "template:reviewer:direct",
            "template:acp_worker:direct",
        ),
        cooldown_s=cooldown,
    )


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def observation(value: dict[str, Any] | None = None) -> Any:
    return roster.observation_from_fixture(value or fixture())


def test_review_backlog_drains_worker_then_re_roles_after_fresh_observation() -> None:
    initial = observation()
    first = roster.create_plan(grant(cap=3), initial, now=NOW)

    assert first["desired"] == {"worker": 1, "reviewer": 2, "verifier": 0}
    assert first["actions"] == [
        {
            "kind": "drain",
            "seat_id": "seat-worker-1",
            "identity_id": "agent-worker-1",
            "state_id": "state-worker-1",
            "state_dir_id": "state-dir-worker-1",
            "generation": 1,
            "target_role": "reviewer",
        }
    ]

    drained = fixture()
    drained["seats"][0]["lifecycle"] = "draining"
    drained["seats"][0]["transition_at"] = "2030-01-01T11:55:00+00:00"
    second = roster.create_plan(grant(cap=3), observation(drained), now=NOW)

    assert second["actions"] == [
        {
            "kind": "re_role",
            "seat_id": "seat-worker-1",
            "identity_id": "agent-worker-1",
            "state_id": "state-worker-1",
            "state_dir_id": "state-dir-worker-1",
            "generation": 1,
            "target_role": "reviewer",
            "template_id": "template:reviewer:direct",
        }
    ]


def test_no_demand_pauses_only_after_drain_and_preserves_floors() -> None:
    value = fixture()
    value["demand"] = {key: 0 for key in value["demand"]}
    value["seats"][0]["lifecycle"] = "draining"
    plan = roster.create_plan(grant(cap=3), observation(value), now=NOW)

    assert plan["desired"] == {"worker": 1, "reviewer": 1, "verifier": 0}
    assert [action["kind"] for action in plan["actions"]] == ["pause"]


def test_gate_budget_uses_headroom_and_releases_gate_heavy_capacity_when_low() -> None:
    high = fixture()
    high["demand"].update(
        {"unassignable_review": 0, "full_gate_queue_depth": 6, "full_gate_oldest_wait_s": 300}
    )
    healthy = roster.create_plan(grant(cap=5), observation(high), now=NOW)
    assert healthy["full_gate_concurrency"] == 3
    assert healthy["desired"]["verifier"] > 0

    low = copy.deepcopy(high)
    low["host"]["memory_headroom_ratio"] = 0.1
    saturated = roster.create_plan(grant(cap=5), observation(low), now=NOW)
    assert saturated["full_gate_concurrency"] == 1
    assert saturated["desired"]["verifier"] == 0


def test_registry_projects_receive_weighted_nonzero_admission_without_provider_assumptions() -> None:
    value = fixture()
    empty = {key: 0 for key in value["demand"]}
    project_a = {**empty, "unassignable_work": 1, "oldest_work_age_s": 600}
    project_b = {**empty, "unassignable_review": 1}
    value["projects"] = [
        {"project_id": "project-a", "priority": 10, "demand": project_a},
        {"project_id": "project-b", "priority": 90, "demand": project_b},
    ]
    value["demand"] = {
        **empty,
        "unassignable_work": 1,
        "unassignable_review": 1,
        "oldest_work_age_s": 600,
    }

    plan = roster.create_plan(grant(cap=4), observation(value), now=NOW)
    admission = {row["project_id"]: row for row in plan["project_admission"]}

    assert admission["project-a"]["share_units"] > 0
    assert admission["project-b"]["share_units"] > admission["project-a"]["share_units"]
    assert admission["project-a"]["role_pressure"]["worker"] == 1
    assert admission["project-b"]["role_pressure"]["reviewer"] == 1

    invalid = copy.deepcopy(value)
    invalid["demand"]["unassignable_work"] = 2
    with pytest.raises(roster.SupervisorPlanError, match="project_demand_aggregate_mismatch"):
        observation(invalid)


def test_cap_is_never_exceeded_and_migration_overage_is_audited() -> None:
    value = fixture()
    value["demand"].update(
        {"unassignable_work": 99, "unassignable_review": 99, "full_gate_queue_depth": 99}
    )
    plan = roster.create_plan(grant(cap=2), observation(value), now=NOW)

    assert sum(plan["desired"].values()) == 2
    assert plan["findings"] == [
        {"reason_code": "observed_seats_exceed_cap", "observed": 3, "host_seat_cap": 2}
    ]
    assert plan["audit"]


def test_lease_and_cooldown_block_destructive_transition() -> None:
    value = fixture()
    value["demand"] = {key: 0 for key in value["demand"]}
    value["seats"][0].update(
        {
            "lifecycle": "draining",
            "work_claim": True,
            "transition_at": "2030-01-01T11:59:30+00:00",
        }
    )
    plan = roster.create_plan(grant(cap=3), observation(value), now=NOW)
    assert all(action.get("seat_id") != "seat-worker-1" for action in plan["actions"])

    cooled = copy.deepcopy(value)
    cooled["seats"][0]["work_claim"] = False
    cooled["seats"][0]["transition_at"] = "2030-01-01T11:50:00+00:00"
    safe = roster.create_plan(grant(cap=3), observation(cooled), now=NOW)
    assert safe["actions"][0]["kind"] == "pause"


def test_confirm_fails_closed_on_drift_and_submits_existing_command_shape() -> None:
    current = observation()
    plan = roster.create_plan(grant(cap=3), current, prior_revision=9, now=NOW)
    confirmed = roster.confirm_plan(plan, grant(cap=3), current, now=NOW)
    assert confirmed["revision"] == 10
    assert confirmed["plan_digest_sha256"] == plan["plan_digest_sha256"]

    changed = fixture()
    changed["demand"]["unassignable_review"] = 3
    with pytest.raises(roster.SupervisorPlanError, match="observation_changed"):
        roster.confirm_plan(plan, grant(cap=3), observation(changed), now=NOW)
    with pytest.raises(roster.SupervisorPlanError, match="authorization_changed"):
        roster.confirm_plan(plan, grant(cap=4), current, now=NOW)

    class Client:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None
            self.state: str | None = json.dumps(
                {**confirmed, "revision": 9}, sort_keys=True, separators=(",", ":")
            )

        async def board_state_get(self, key: str) -> dict[str, Any]:
            assert key == roster.ROSTER_STATE_KEY
            assert self.state is not None
            return {"state": {"value": self.state}}

        async def board_state_update(
            self, key: str, value: str, *, expected_sha256: str | None = None
        ) -> dict[str, Any]:
            assert key == roster.ROSTER_STATE_KEY
            assert expected_sha256 == roster.hashlib.sha256(
                self.state.encode("utf-8")
            ).hexdigest()
            self.state = value
            return {"ok": True}

        async def butler_command_submit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.call = {"args": args, "kwargs": kwargs}
            return {"ok": True}

    client = Client()
    output, response = asyncio.run(
        roster.confirm_with_command(client, plan, grant(cap=3), current, now=NOW)
    )
    assert output == confirmed
    assert response == {"ok": True}
    assert client.call is not None
    assert client.call["kwargs"]["sender_channel"] == "a2a"
    assert client.call["kwargs"]["intent"] == "set_desired_state"
    assert client.call["kwargs"]["parameters"] == {
        "desired_revision": 10,
        "desired_digest_sha256": roster.digest(confirmed),
    }
    assert client.call["kwargs"]["expected_config_revision"] == 7
    assert client.call["kwargs"]["expires_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert json.loads(client.state or "null") == confirmed


def test_grant_uses_single_host_cap_and_matching_envelope_authorization() -> None:
    budget = {
        "period": "day",
        "max_tokens": 1000,
        "max_cost_microunits": 1000,
        "max_external_calls": 10,
    }
    config = {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": "pursers",
        "revision": 7,
        "enabled": True,
        "host_runtime": {
            "host_ref": "host:one",
            "revision": 1,
            "agent_process_ceiling": 15,
            "control_plane_processes": 2,
            "total_process_ceiling": 17,
            "configured_by": "operator",
            "configured_at": NOW.isoformat(),
        },
        "desired": {
            "mode": "autonomous",
            "runner": "direct_api",
            "capacity": {
                "worker": {"min": 0, "target": 1, "max": 8},
                "reviewer": {"min": 0, "target": 1, "max": 6},
                "acp_worker": {"min": 0, "target": 0, "max": 1},
            },
            "host_concurrency": 15,
            "board_concurrency": 15,
            "cooldowns": {
                "scale_up_s": 30,
                "scale_down_s": 90,
                "failure_backoff_s": 10,
            },
            "budget": budget,
            "connectors": [],
        },
        "envelope": {
            "fingerprint_sha256": FINGERPRINT,
            "approved_template_ids": [
                "template:worker:direct",
                "template:reviewer:direct",
                "template:acp_worker:direct",
            ],
            "approved_connector_ids": [],
            "max_capacity": {"worker": 8, "reviewer": 6, "acp_worker": 1},
            "max_host_concurrency": 3,
            "max_board_concurrency": 15,
            "max_budget": budget,
            "created_by": "operator",
            "created_at": NOW.isoformat(),
        },
        "authorization": {
            "authorization_id": "auth:one",
            "config_revision": 7,
            "envelope_fingerprint_sha256": FINGERPRINT,
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    }
    resolved = roster.grant_from_config(config, NOW)
    assert resolved.host_seat_cap == 15
    assert resolved.gate_concurrency_ceiling == 3
    assert resolved.cooldown_s == 90

    config["authorization"]["envelope_fingerprint_sha256"] = "b" * 64
    with pytest.raises(roster.SupervisorPlanError, match="authorization_invalid"):
        roster.grant_from_config(config, NOW)


def test_config_schema_and_observation_freshness_fail_closed() -> None:
    value = fixture()
    current = observation(value)
    with pytest.raises(roster.SupervisorPlanError, match="observation_stale"):
        roster.create_plan(
            grant(), current, now=NOW + timedelta(seconds=roster.MAX_OBSERVATION_AGE_S + 1)
        )

    # The schema validator rejects fields that the old shallow parser ignored.
    budget = {
        "period": "day",
        "max_tokens": 0,
        "max_cost_microunits": 0,
        "max_external_calls": 0,
    }
    invalid = {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": "pursers",
        "revision": 1,
        "enabled": True,
        "host_runtime": {},
        "desired": {"mode": "autonomous", "budget": budget},
        "envelope": {},
        "authorization": {},
        "unexpected": True,
    }
    with pytest.raises(roster.SupervisorPlanError, match="config_schema_invalid"):
        roster.grant_from_config(invalid, NOW)


def test_provision_binds_approved_template_and_never_reuses_identity_state() -> None:
    value = fixture()
    value["seats"] = []
    value["demand"].update({"unassignable_work": 5, "unassignable_review": 2})
    current = observation(value)
    first = roster.create_plan(grant(cap=4), current, prior_revision=0, now=NOW)
    second = roster.create_plan(grant(cap=4), current, prior_revision=1, now=NOW)
    provisions = [row for row in first["actions"] if row["kind"] == "provision"]
    assert provisions
    for row in provisions:
        assert row["template_id"] in grant().approved_template_ids
        assert row["generation"] == 1
        assert row["identity_id"].startswith("identity:")
        assert row["state_id"].startswith("state:")
        assert row["state_dir_id"].startswith("state-dir:")
    for field in ("seat_id", "identity_id", "state_id", "state_dir_id"):
        assert len({row[field] for row in provisions}) == len(provisions)
        assert {row[field] for row in provisions}.isdisjoint(
            {row[field] for row in second["actions"] if row["kind"] == "provision"}
        )


def test_supervisor_reads_canonical_roster_or_absent_only_legacy_fallback() -> None:
    legacy = {"ROSTER": "worker-1 reviewer-1", "MAX": 2}
    source, selected = roster.roster_or_legacy(None, legacy)
    assert source == "legacy"
    assert selected == legacy

    current = observation()
    plan = roster.create_plan(grant(cap=3), current, now=NOW)
    confirmed = roster.confirm_plan(plan, grant(cap=3), current, now=NOW)
    source, selected = roster.roster_or_legacy(
        json.dumps(confirmed, sort_keys=True, separators=(",", ":")), legacy
    )
    assert source == "canonical"
    assert selected["revision"] == 1
    with pytest.raises(roster.SupervisorPlanError, match="roster_state_invalid"):
        roster.roster_or_legacy("{}", legacy)
