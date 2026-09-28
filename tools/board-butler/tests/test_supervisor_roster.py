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
        cooldown_s=cooldown,
    )


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def observation(value: dict[str, Any] | None = None) -> Any:
    return roster.observation_from_fixture(value or fixture())


def test_review_backlog_drains_worker_then_re_roles_after_fresh_observation() -> None:
    initial = observation()
    first = roster.create_plan(grant(cap=3), initial)

    assert first["desired"] == {"worker": 1, "reviewer": 2, "verifier": 0}
    assert first["actions"] == [
        {
            "kind": "drain",
            "seat_id": "seat-worker-1",
            "identity_id": "agent-worker-1",
            "state_id": "state-worker-1",
            "target_role": "reviewer",
        }
    ]

    drained = fixture()
    drained["seats"][0]["lifecycle"] = "draining"
    drained["seats"][0]["transition_at"] = "2030-01-01T11:55:00+00:00"
    second = roster.create_plan(grant(cap=3), observation(drained))

    assert second["actions"] == [
        {
            "kind": "re_role",
            "seat_id": "seat-worker-1",
            "identity_id": "agent-worker-1",
            "state_id": "state-worker-1",
            "target_role": "reviewer",
        }
    ]


def test_no_demand_pauses_only_after_drain_and_preserves_floors() -> None:
    value = fixture()
    value["demand"] = {key: 0 for key in value["demand"]}
    value["seats"][0]["lifecycle"] = "draining"
    plan = roster.create_plan(grant(cap=3), observation(value))

    assert plan["desired"] == {"worker": 1, "reviewer": 1, "verifier": 0}
    assert [action["kind"] for action in plan["actions"]] == ["pause"]


def test_gate_budget_uses_headroom_and_releases_gate_heavy_capacity_when_low() -> None:
    high = fixture()
    high["demand"].update(
        {"unassignable_review": 0, "full_gate_queue_depth": 6, "full_gate_oldest_wait_s": 300}
    )
    healthy = roster.create_plan(grant(cap=5), observation(high))
    assert healthy["full_gate_concurrency"] == 3
    assert healthy["desired"]["verifier"] > 0

    low = copy.deepcopy(high)
    low["host"]["memory_headroom_ratio"] = 0.1
    saturated = roster.create_plan(grant(cap=5), observation(low))
    assert saturated["full_gate_concurrency"] == 1
    assert saturated["desired"]["verifier"] == 0


def test_cap_is_never_exceeded_and_migration_overage_is_audited() -> None:
    value = fixture()
    value["demand"].update(
        {"unassignable_work": 99, "unassignable_review": 99, "full_gate_queue_depth": 99}
    )
    plan = roster.create_plan(grant(cap=2), observation(value))

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
    plan = roster.create_plan(grant(cap=3), observation(value))
    assert all(action.get("seat_id") != "seat-worker-1" for action in plan["actions"])

    cooled = copy.deepcopy(value)
    cooled["seats"][0]["work_claim"] = False
    cooled["seats"][0]["transition_at"] = "2030-01-01T11:50:00+00:00"
    safe = roster.create_plan(grant(cap=3), observation(cooled))
    assert safe["actions"][0]["kind"] == "pause"


def test_confirm_fails_closed_on_drift_and_submits_existing_command_shape() -> None:
    current = observation()
    plan = roster.create_plan(grant(cap=3), current, prior_revision=9)
    confirmed = roster.confirm_plan(plan, grant(cap=3), current)
    assert confirmed["revision"] == 10
    assert confirmed["plan_digest_sha256"] == plan["plan_digest_sha256"]

    changed = fixture()
    changed["demand"]["unassignable_review"] = 3
    with pytest.raises(roster.SupervisorPlanError, match="observation_changed"):
        roster.confirm_plan(plan, grant(cap=3), observation(changed))
    with pytest.raises(roster.SupervisorPlanError, match="authorization_changed"):
        roster.confirm_plan(plan, grant(cap=4), current)

    class Client:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None

        async def butler_command_submit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.call = {"args": args, "kwargs": kwargs}
            return {"ok": True}

    client = Client()
    output, response = asyncio.run(
        roster.confirm_with_command(client, plan, grant(cap=3), current)
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


def test_grant_uses_single_host_cap_and_matching_envelope_authorization() -> None:
    config = {
        "schema": "autonomous_butler_config_v1",
        "board_id": "pursers",
        "revision": 7,
        "enabled": True,
        "host_runtime": {"agent_process_ceiling": 15},
        "desired": {"mode": "autonomous", "cooldowns": {"scale_down_s": 90}},
        "envelope": {
            "fingerprint_sha256": FINGERPRINT,
            "max_capacity": {"worker": 1, "reviewer": 1, "acp_worker": 0},
        },
        "authorization": {
            "config_revision": 7,
            "envelope_fingerprint_sha256": FINGERPRINT,
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    }
    resolved = roster.grant_from_config(config, NOW)
    assert resolved.host_seat_cap == 15
    assert resolved.cooldown_s == 90

    config["authorization"]["envelope_fingerprint_sha256"] = "b" * 64
    with pytest.raises(roster.SupervisorPlanError, match="authorization_invalid"):
        roster.grant_from_config(config, NOW)
