from __future__ import annotations

import copy
import importlib.util
import asyncio
import json
import os
import socket
import sys
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


BUTLER_PATH = Path(__file__).resolve().parents[1] / "board_butler.py"
BUTLER_SPEC = importlib.util.spec_from_file_location("board_butler_fleet", BUTLER_PATH)
assert BUTLER_SPEC and BUTLER_SPEC.loader
butler = importlib.util.module_from_spec(BUTLER_SPEC)
sys.modules[BUTLER_SPEC.name] = butler
BUTLER_SPEC.loader.exec_module(butler)

EXECUTOR_PATH = Path(__file__).resolve().parents[2] / "seat-kit" / "fleet_executor.py"
EXECUTOR_SPEC = importlib.util.spec_from_file_location("fleet_executor_integration", EXECUTOR_PATH)
assert EXECUTOR_SPEC and EXECUTOR_SPEC.loader
fleet_executor = importlib.util.module_from_spec(EXECUTOR_SPEC)
sys.modules[EXECUTOR_SPEC.name] = fleet_executor
EXECUTOR_SPEC.loader.exec_module(fleet_executor)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64
DIGEST = "b" * 64


def role_policy(maximum: int = 4) -> dict[str, Any]:
    return {
        role: butler.FleetRolePolicy(0, 1, maximum, 1)
        for role in butler.FLEET_ROLES
    }


def board_policy(
    board_id: str = "pursers",
    *,
    maximum: int = 8,
    provider_maximums: Mapping[str, int] | None = None,
    idle_grace_s: int = 60,
) -> Any:
    providers = dict(provider_maximums or {"direct": maximum})
    return butler.FleetBoardPolicy(
        board_id=board_id,
        roles=role_policy(),
        board_maximum=maximum,
        provider_maximums=providers,
        approved_template_ids=frozenset(
            f"template:{role}:{provider}"
            for role in butler.FLEET_ROLES
            for provider in providers
        ),
        idle_grace_s=idle_grace_s,
        scale_up_cooldown_s=30,
        scale_down_cooldown_s=30,
        failure_backoff_s=5,
        provider_latency_limit_ms=1_000,
    )


def demand(
    board_id: str = "pursers",
    *,
    work: int = 0,
    review: int = 0,
    acp: int = 0,
    tier: int = 1,
    health: Mapping[str, str] | None = None,
    latency: Mapping[str, int] | None = None,
    age: int = 0,
    expiring: int = 0,
) -> Any:
    return butler.FleetDemand(
        board_id=board_id,
        open_by_tier={tier: work},
        review_backlog=review,
        acp_backlog=acp,
        oldest_ticket_age_s=age,
        expiring_offers=expiring,
        provider_health=dict(health or {"direct": "healthy"}),
        provider_latency_ms=dict(latency or {"direct": 10}),
    )


def seat(
    seat_id: str,
    role: str,
    *,
    board_id: str = "pursers",
    provider: str = "direct",
    lifecycle: str = "stopped",
    busy: bool = False,
    live: bool = False,
    transition_at: datetime | None = None,
    template_id: str | None = None,
    template_digest: str = DIGEST,
    managed: bool = True,
) -> Any:
    return butler.FleetSeat(
        seat_id=seat_id,
        board_id=board_id,
        role=role,
        provider=provider,
        template_id=template_id or f"template:{role}:{provider}",
        template_digest_sha256=template_digest,
        generation=1,
        lifecycle=lifecycle,
        ready=lifecycle in {"ready", "busy"},
        busy=busy,
        live_lease=live,
        transition_at=transition_at or NOW - timedelta(minutes=10),
        managed=managed,
    )


def snapshot(
    demands: Mapping[str, Any],
    seats: list[Any],
    *,
    now: datetime = NOW,
    load: float = 0.2,
    capacity: bool = True,
) -> Any:
    return butler.FleetSnapshot(
        observed_at=now,
        demands=demands,
        seats=tuple(seats),
        host_load_ratio=load,
        host_capacity_available=capacity,
        executor_healthy=True,
    )


def reconciler(
    policies: Mapping[str, Any] | None = None,
    *,
    host_cap: int = 8,
    config_revision: int = 7,
    fingerprint: str = FINGERPRINT,
) -> Any:
    return butler.FleetReconciler(
        butler.FleetHostPolicy(host_cap, 2, host_cap + 2),
        dict(policies or {"pursers": board_policy()}),
        config_revision=config_revision,
        authorization_fingerprint_sha256=fingerprint,
    )


def active_config(board_id: str = "pursers") -> dict[str, Any]:
    counts = {
        "worker": {"min": 0, "target": 1, "max": 2},
        "reviewer": {"min": 0, "target": 1, "max": 1},
        "acp_worker": {"min": 0, "target": 1, "max": 1},
    }
    return {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": board_id,
        "revision": 3,
        "enabled": True,
        "host_runtime": {
            "host_ref": "host:one",
            "revision": 2,
            "agent_process_ceiling": 4,
            "control_plane_processes": 2,
            "total_process_ceiling": 6,
        },
        "desired": {
            "mode": "autonomous",
            "capacity": counts,
            "host_concurrency": 4,
            "board_concurrency": 4,
            "cooldowns": {
                "scale_up_s": 5,
                "scale_down_s": 60,
                "failure_backoff_s": 5,
            },
        },
        "envelope": {
            "fingerprint_sha256": FINGERPRINT,
            "approved_template_ids": [
                "template:worker:direct",
                "template:reviewer:direct",
                "template:acp_worker:direct",
            ],
            "max_capacity": {"worker": 2, "reviewer": 1, "acp_worker": 1},
            "max_host_concurrency": 4,
            "max_board_concurrency": 4,
        },
        "authorization": {
            "config_revision": 3,
            "envelope_fingerprint_sha256": FINGERPRINT,
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    }


def seats_for_board(
    board_id: str = "pursers", *, provider: str = "direct", count: int = 4
) -> list[Any]:
    result = []
    for role in butler.FLEET_ROLES:
        result.extend(
            seat(f"{board_id}:{role}:{index}", role, board_id=board_id, provider=provider)
            for index in range(count)
        )
    return result


def test_burst_scales_workers_and_reviewers_without_creating_review_bottleneck() -> None:
    current = snapshot(
        {"pursers": demand(work=20, review=3, acp=2, age=600, expiring=2)},
        seats_for_board(),
    )
    plan = reconciler(host_cap=8).plan(current, {})

    assert plan.desired["pursers"] == {
        "worker": 4,
        "reviewer": 3,
        "acp_worker": 1,
    }
    assert len(plan.operations) == 8
    assert {operation.action for operation in plan.operations} == {"start"}
    assert sum(plan.desired["pursers"].values()) == 8


def test_active_config_derives_only_human_authorized_bounds() -> None:
    host, policies = butler.fleet_policies_from_config(
        {"pursers": active_config()}, {"pursers": {"direct": 4}}, NOW
    )

    assert host.role_capacity == 4
    assert policies["pursers"].board_maximum == 4
    assert policies["pursers"].roles["worker"].maximum == 2
    assert policies["pursers"].approved_template_ids == {
        "template:worker:direct",
        "template:reviewer:direct",
        "template:acp_worker:direct",
    }

    expired = active_config()
    expired["authorization"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(butler.ButlerConfigError, match="authorization"):
        butler.fleet_policies_from_config(
            {"pursers": expired}, {"pursers": {"direct": 4}}, NOW
        )


def test_product_snapshot_selector_consumes_real_board_shaped_state() -> None:
    selected = butler.fleet_snapshot_from_products(
        {
            "pursers": {
                "truncated": False,
                "tickets": [
                    {
                        "ticket_id": "TK-work",
                        "status": "open",
                        "tier": 2,
                        "tags": [],
                        "created_at": (NOW - timedelta(minutes=10)).isoformat(),
                        "dispatch_state": {
                            "state": "offered",
                            "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                        },
                    },
                    {
                        "ticket_id": "TK-review",
                        "status": "submitted",
                        "tier": 1,
                        "tags": [],
                    },
                ],
                "agents": [
                    {
                        "agent_id": "AI-worker-a",
                        "agent_name": "worker-a",
                        "lifecycle_status": "active",
                        "status": "busy",
                        "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                    }
                ],
            }
        },
        [
            {
                "seat_id": "worker-a",
                "board_id": "pursers",
                "role": "worker",
                "provider": "direct",
                "template_id": "template:worker:direct",
                "template_digest_sha256": DIGEST,
                "generation": 1,
                "lifecycle": "busy",
                "transition_at": (NOW - timedelta(minutes=1)).isoformat(),
                "managed": True,
            }
        ],
        {"pursers": {"direct": {"status": "healthy", "latency_ms": 15}}},
        {
            "load_ratio": 0.4,
            "capacity_available": True,
            "executor_status": "healthy",
        },
        NOW,
    )

    assert selected.demands["pursers"].work_pressure == 6
    assert selected.demands["pursers"].review_backlog == 1
    assert selected.seats[0].live_lease is True
    assert selected.seats[0].ready is True


def test_product_snapshot_selector_rejects_truncated_ticket_input() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        butler.fleet_snapshot_from_products(
            {"pursers": {"truncated": True, "tickets": [], "agents": []}},
            [],
            {"pursers": {"direct": {"status": "healthy", "latency_ms": 1}}},
            {
                "load_ratio": 0.1,
                "capacity_available": True,
                "executor_status": "healthy",
            },
            NOW,
        )


def test_registry_host_cap_prioritizes_independent_review_across_boards() -> None:
    policies = {
        "alpha": board_policy("alpha", maximum=6),
        "beta": board_policy("beta", maximum=6),
    }
    current = snapshot(
        {
            "alpha": demand("alpha", work=20, review=1),
            "beta": demand("beta", work=20, review=1),
        },
        seats_for_board("alpha") + seats_for_board("beta"),
    )
    plan = reconciler(policies, host_cap=5).plan(current, {})

    assert sum(sum(row.values()) for row in plan.desired.values()) == 5
    assert plan.desired["alpha"]["reviewer"] >= 1
    assert plan.desired["beta"]["reviewer"] >= 1


def test_provider_caps_health_and_latency_are_hard_constraints() -> None:
    policy = board_policy(
        maximum=8, provider_maximums={"fast": 1, "slow": 4, "down": 4}
    )
    current = snapshot(
        {
            "pursers": demand(
                work=20,
                health={"fast": "healthy", "slow": "healthy", "down": "unavailable"},
                latency={"fast": 10, "slow": 2_000, "down": 1},
            )
        },
        [
            seat("fast-worker", "worker", provider="fast"),
            seat("slow-worker", "worker", provider="slow"),
            seat("down-worker", "worker", provider="down"),
        ],
    )
    plan = reconciler({"pursers": policy}).plan(current, {})

    assert plan.desired["pursers"]["worker"] == 1
    assert plan.provider_desired["pursers"] == {"down": 0, "fast": 1, "slow": 0}
    assert [(item.action, item.seat_id) for item in plan.operations] == [
        ("start", "fast-worker")
    ]


def test_idle_grace_hysteresis_then_drain_then_stop() -> None:
    policy = board_policy(idle_grace_s=60)
    ready = seat("worker-a", "worker", lifecycle="ready")
    prior = {
        "config_revision": 7,
        "boards": {
            "pursers": {
                "desired": {"worker": 1, "reviewer": 0, "acp_worker": 0},
                "idle_since": (NOW - timedelta(seconds=30)).isoformat(),
            }
        },
    }
    engine = reconciler({"pursers": policy})

    held = engine.plan(snapshot({"pursers": demand()}, [ready]), prior)
    assert held.desired["pursers"]["worker"] == 1
    assert held.operations == ()

    prior["boards"]["pursers"]["idle_since"] = (
        NOW - timedelta(seconds=120)
    ).isoformat()
    draining = engine.plan(snapshot({"pursers": demand()}, [ready]), prior)
    assert [(item.action, item.seat_id) for item in draining.operations] == [
        ("drain", "worker-a")
    ]

    drained_seat = seat("worker-a", "worker", lifecycle="draining")
    stopped = engine.plan(snapshot({"pursers": demand()}, [drained_seat]), prior)
    assert [(item.action, item.seat_id) for item in stopped.operations] == [
        ("stop", "worker-a")
    ]


def test_live_lease_is_never_drained_or_stopped_when_demand_drops_to_zero() -> None:
    holder = seat("worker-live", "worker", lifecycle="busy", busy=True, live=True)
    plan = reconciler().plan(snapshot({"pursers": demand()}, [holder]), {})

    assert plan.desired["pursers"]["worker"] == 1
    assert plan.operations == ()


def test_existing_live_oversubscription_never_raises_desired_above_hard_cap() -> None:
    policy = board_policy(maximum=1, provider_maximums={"direct": 1})
    holders = [
        seat("worker-live-a", "worker", lifecycle="busy", busy=True, live=True),
        seat("worker-live-b", "worker", lifecycle="busy", busy=True, live=True),
    ]
    plan = reconciler({"pursers": policy}, host_cap=1).plan(
        snapshot({"pursers": demand()}, holders), {}
    )

    assert sum(plan.desired["pursers"].values()) == 1
    assert plan.operations == ()


def test_first_zero_demand_observation_starts_grace_instead_of_draining() -> None:
    idle = seat("worker-idle", "worker", lifecycle="ready")
    plan = reconciler().plan(snapshot({"pursers": demand()}, [idle]), {})

    assert plan.desired["pursers"]["worker"] == 1
    assert plan.operations == ()


def test_unmanaged_or_unapproved_seats_are_never_mutated() -> None:
    unmanaged = seat("manual-worker", "worker", lifecycle="ready", managed=False)
    unapproved = seat(
        "unknown-worker",
        "worker",
        template_id="template:unapproved",
    )
    plan = reconciler().plan(
        snapshot({"pursers": demand(work=10)}, [unmanaged, unapproved]), {}
    )

    assert sum(plan.desired["pursers"].values()) == 0
    assert plan.operations == ()


def test_unmanaged_active_seat_consumes_board_role_and_provider_headroom() -> None:
    policy = board_policy(maximum=2, provider_maximums={"direct": 2})
    current = snapshot(
        {"pursers": demand(work=20)},
        [
            seat("manual-worker", "worker", lifecycle="ready", managed=False),
            seat("worker-a", "worker"),
            seat("worker-b", "worker"),
        ],
    )

    plan = reconciler({"pursers": policy}, host_cap=8).plan(current, {})

    assert [(item.action, item.seat_id) for item in plan.operations] == [
        ("start", "worker-a")
    ]


def test_high_host_load_suppresses_worker_burst_but_preserves_review_capacity() -> None:
    holder = seat("worker-live", "worker", lifecycle="busy", busy=True, live=True)
    candidates = [holder, *seats_for_board(count=2)]
    plan = reconciler(host_cap=8).plan(
        snapshot({"pursers": demand(work=20, review=3)}, candidates, load=0.99),
        {},
    )

    assert sum(plan.desired["pursers"].values()) == 1
    assert plan.desired["pursers"]["worker"] == 0
    assert plan.desired["pursers"]["reviewer"] == 1
    assert plan.operations == ()


class RecordingExecutor:
    def __init__(self, fail_seat: str | None = None) -> None:
        self.fail_seat = fail_seat
        self.calls: list[Any] = []

    def execute(self, operation: Any) -> Mapping[str, Any]:
        self.calls.append(operation)
        if operation.seat_id == self.fail_seat:
            raise OSError("host unavailable")
        return {
            "operation_id": operation.operation_id,
            "outcome": "succeeded",
            "committed": True,
        }


def test_partial_host_failure_is_isolated_and_retry_state_is_auditable() -> None:
    current = snapshot(
        {"pursers": demand(work=2)},
        [seat("worker-a", "worker"), seat("worker-b", "worker")],
    )
    store = butler.MemoryFleetStateStore()
    executor = RecordingExecutor(fail_seat="worker-a")

    report = reconciler().reconcile(current, store, executor)

    assert [row["outcome"] for row in report["receipts"]] == ["unknown", "succeeded"]
    _, durable = store.load()
    rows = durable["operations"]
    assert rows[executor.calls[0].operation_id]["status"] == "unknown"
    assert rows[executor.calls[1].operation_id]["status"] == "terminal"


def test_crash_recovery_reuses_operation_key_and_executor_replay_boundary() -> None:
    current = snapshot(
        {"pursers": demand(work=1)}, [seat("worker-a", "worker")]
    )
    store = butler.MemoryFleetStateStore()
    first = RecordingExecutor(fail_seat="worker-a")
    engine = reconciler()
    engine.reconcile(current, store, first)

    second = RecordingExecutor()
    retry = snapshot(
        {"pursers": demand(work=1)},
        [seat("worker-a", "worker")],
        now=NOW + timedelta(seconds=6),
    )
    engine.reconcile(retry, store, second)

    assert first.calls[0].operation_id == second.calls[0].operation_id
    _, durable = store.load()
    assert durable["operations"][second.calls[0].operation_id]["attempts"] == 2


class ContendedStore(butler.MemoryFleetStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def compare_and_swap(self, expected_revision: int, value: Mapping[str, Any]) -> bool:
        if self.failures:
            self.failures -= 1
            return False
        return super().compare_and_swap(expected_revision, value)


def test_cas_conflict_is_bounded_and_replanned() -> None:
    current = snapshot(
        {"pursers": demand(work=1)}, [seat("worker-a", "worker")]
    )
    store = ContendedStore()
    executor = RecordingExecutor()

    report = reconciler().reconcile(current, store, executor)

    assert len(report["operations"]) == 1
    assert store.revision == 2


def test_file_state_store_survives_restart_and_enforces_cas(tmp_path: Path) -> None:
    path = (tmp_path / "state" / "fleet.json").resolve()
    first = butler.FileFleetStateStore(path)
    assert first.load() == (0, {})
    assert first.compare_and_swap(0, {"config_revision": 7}) is True

    restarted = butler.FileFleetStateStore(path)
    assert restarted.load() == (1, {"config_revision": 7})
    assert restarted.compare_and_swap(0, {"config_revision": 8}) is False
    assert restarted.compare_and_swap(1, {"config_revision": 7, "replayed": True}) is True
    assert path.stat().st_mode & 0o777 == 0o600


def test_policy_transition_is_atomic_and_does_not_replay_stale_operations(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state" / "fleet.json").resolve()
    store = butler.FileFleetStateStore(path)
    first = reconciler(
        {"pursers": board_policy(maximum=2, provider_maximums={"direct": 2})},
        host_cap=2,
    )
    old_executor = RecordingExecutor(fail_seat="worker-a")
    first.reconcile(
        snapshot(
            {"pursers": demand(work=2)},
            [seat("worker-a", "worker"), seat("worker-b", "worker")],
        ),
        store,
        old_executor,
    )
    old_unknown_id = old_executor.calls[0].operation_id
    old_terminal_id = old_executor.calls[1].operation_id

    new_fingerprint = "c" * 64
    second = reconciler(
        {
            "pursers": board_policy(
                maximum=1,
                provider_maximums={"direct": 1},
                idle_grace_s=0,
            )
        },
        host_cap=1,
        config_revision=8,
        fingerprint=new_fingerprint,
    )

    class InspectingExecutor(RecordingExecutor):
        def execute(self, operation: Any) -> Mapping[str, Any]:
            _revision, durable = store.load()
            assert durable["config_revisions"] == {"pursers": 8}
            assert durable["authorization_fingerprints"] == {
                "pursers": new_fingerprint
            }
            assert durable["boards"]["pursers"]["desired"]["worker"] == 1
            assert durable["operations"][operation.operation_id]["status"] == "pending"
            assert old_unknown_id not in durable["operations"]
            return super().execute(operation)

    new_executor = InspectingExecutor()
    report = second.reconcile(
        snapshot(
            {"pursers": demand(work=1)},
            [
                seat("worker-a", "worker", lifecycle="ready"),
                seat("worker-b", "worker", lifecycle="ready"),
            ],
            now=NOW + timedelta(minutes=1),
        ),
        butler.FileFleetStateStore(path),
        new_executor,
    )

    assert [(item.action, item.seat_id) for item in new_executor.calls] == [
        ("drain", "worker-b")
    ]
    assert new_executor.calls[0].authorization_fingerprint_sha256 == new_fingerprint
    assert old_unknown_id not in report["operations"]
    _revision, durable = butler.FileFleetStateStore(path).load()
    assert old_unknown_id not in durable["operations"]
    assert durable["operations"][old_terminal_id]["status"] == "terminal"
    assert durable["policy_transition"]["discarded_nonterminal_operations"] == 1
    assert durable["policy_transition"]["preserved_terminal_operations"] == 1
    assert sum(durable["boards"]["pursers"]["desired"].values()) == 1


def test_stale_policy_revision_cannot_roll_back_durable_caps(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state" / "fleet.json").resolve()
    store = butler.FileFleetStateStore(path)
    current = snapshot(
        {"pursers": demand(work=1)},
        [
            seat("worker-a", "worker", lifecycle="ready"),
            seat("worker-b", "worker"),
        ],
    )
    current_policy = reconciler(
        {"pursers": board_policy(maximum=1, provider_maximums={"direct": 1})},
        host_cap=1,
        config_revision=8,
        fingerprint="c" * 64,
    )
    current_policy.reconcile(current, store, RecordingExecutor())
    before = store.load()

    stale_executor = RecordingExecutor()
    report = reconciler(
        {"pursers": board_policy(maximum=2, provider_maximums={"direct": 2})},
        host_cap=2,
        config_revision=7,
    ).reconcile(
        snapshot(
            {"pursers": demand(work=2)},
            [
                seat("worker-a", "worker", lifecycle="ready"),
                seat("worker-b", "worker"),
            ],
            now=NOW + timedelta(minutes=1),
        ),
        butler.FileFleetStateStore(path),
        stale_executor,
    )

    assert report["status"] == "shadow"
    assert report["effective_state"] == "shadow"
    assert report["reason_code"] == "stale_config_revision"
    assert report["desired"]["pursers"]["worker"] == 1
    assert report["operations"] == []
    assert report["receipts"] == []
    assert report["audit_evidence"] == [
        {
            "board_id": "pursers",
            "action": "reconcile",
            "outcome": "denied",
            "reason_code": "stale_config_revision",
            "current_revision": 7,
            "durable_revision": 8,
            "observed_at": (NOW + timedelta(minutes=1)).isoformat(),
            "detail_redacted": True,
        }
    ]
    assert stale_executor.calls == []
    assert butler.FileFleetStateStore(path).load() == before


class PolicyRaceStore(butler.MemoryFleetStateStore):
    def __init__(self, newer: Mapping[str, Any]) -> None:
        super().__init__()
        self.newer = copy.deepcopy(dict(newer))
        self.cas_calls = 0

    def compare_and_swap(
        self, expected_revision: int, value: Mapping[str, Any]
    ) -> bool:
        self.cas_calls += 1
        if self.cas_calls == 1:
            assert super().compare_and_swap(expected_revision, self.newer)
            return False
        return super().compare_and_swap(expected_revision, value)


def test_stale_plan_losing_cas_cannot_overwrite_newer_policy() -> None:
    seed_store = butler.MemoryFleetStateStore()
    reconciler(
        {"pursers": board_policy(maximum=1, provider_maximums={"direct": 1})},
        host_cap=1,
        config_revision=8,
        fingerprint="c" * 64,
    ).reconcile(
        snapshot(
            {"pursers": demand(work=1)},
            [seat("worker-a", "worker", lifecycle="ready")],
        ),
        seed_store,
        RecordingExecutor(),
    )
    _revision, newer = seed_store.load()
    store = PolicyRaceStore(newer)
    executor = RecordingExecutor()

    report = reconciler(
        {"pursers": board_policy(maximum=2, provider_maximums={"direct": 2})},
        host_cap=2,
        config_revision=7,
    ).reconcile(
        snapshot(
            {"pursers": demand(work=2)},
            [seat("worker-a", "worker"), seat("worker-b", "worker")],
            now=NOW + timedelta(minutes=1),
        ),
        store,
        executor,
    )

    assert report["status"] == "shadow"
    assert report["effective_state"] == "shadow"
    assert report["reason_code"] == "stale_config_revision"
    assert store.cas_calls == 1
    assert executor.calls == []
    _revision, durable = store.load()
    assert durable["config_revisions"] == {"pursers": 8}
    assert durable["boards"]["pursers"]["desired"]["worker"] == 1


def test_fingerprint_only_transition_discards_unknown_operation() -> None:
    store = butler.MemoryFleetStateStore()
    failed = RecordingExecutor(fail_seat="worker-a")
    reconciler().reconcile(
        snapshot(
            {"pursers": demand(work=1)},
            [seat("worker-a", "worker")],
        ),
        store,
        failed,
    )
    stale_id = failed.calls[0].operation_id

    executor = RecordingExecutor()
    reconciler(fingerprint="d" * 64).reconcile(
        snapshot(
            {"pursers": demand()},
            [],
            now=NOW + timedelta(minutes=1),
        ),
        store,
        executor,
    )

    assert executor.calls == []
    _revision, durable = store.load()
    assert stale_id not in durable["operations"]
    assert durable["authorization_fingerprints"] == {"pursers": "d" * 64}
    assert durable["policy_transition"]["discarded_nonterminal_operations"] == 1


def test_count_invariants_hold_across_burst_matrix() -> None:
    policy = board_policy(maximum=5, provider_maximums={"direct": 4})
    engine = reconciler({"pursers": policy}, host_cap=3)
    pool = seats_for_board(count=5)
    for work in range(7):
        for review in range(4):
            for acp in range(3):
                current = snapshot(
                    {"pursers": demand(work=work, review=review, acp=acp)}, pool
                )
                plan = engine.plan(current, {})
                counts = plan.desired["pursers"]
                assert all(0 <= counts[role] <= policy.roles[role].maximum for role in butler.FLEET_ROLES)
                assert sum(counts.values()) <= 3
                assert sum(plan.provider_desired["pursers"].values()) <= 4
                assert all(operation.action == "start" for operation in plan.operations)


def test_desired_state_document_matches_landed_strict_schema() -> None:
    current = snapshot(
        {"pursers": demand(work=1)}, [seat("worker-a", "worker")]
    )
    engine = reconciler()
    plan = engine.plan(current, {})
    document = engine.desired_state_document("pursers", current, plan)
    schema = json.loads(
        (ROOT / "docs/design/schemas/autonomous-butler-state-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(document)


class FakeServiceAdapter:
    def __init__(self) -> None:
        self.observation = fleet_executor.ServiceObservation(False, False, False, True)

    def inspect(self, seat_id: str, template: Any) -> Any:
        return self.observation

    def instantiate(self, seat_id: str, template: Any) -> None:
        self.observation = fleet_executor.ServiceObservation(True, False, False, True)

    def start(self, seat_id: str, template: Any) -> None:
        self.observation = fleet_executor.ServiceObservation(
            True, True, True, True, f"fake:{seat_id}:1"
        )

    def drain(self, seat_id: str, template: Any) -> None:
        pass

    def stop(self, seat_id: str, template: Any) -> None:
        self.observation = fleet_executor.ServiceObservation(True, False, False, True)


class KnownLease:
    def observe(self, board_id: str, seat_id: str) -> Any:
        return fleet_executor.LeaseObservation(True)


class ReadyRegistry:
    def observe(self, board_id: str, seat_id: str, template: Any) -> Any:
        return fleet_executor.RegistryReadinessObservation(True, True, (board_id,))


class ReceiptSink:
    def __init__(self) -> None:
        self.receipts: list[Mapping[str, Any]] = []

    def publish(self, receipt: Mapping[str, Any]) -> None:
        self.receipts.append(dict(receipt))


def test_real_executor_integration_starts_approved_seat(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "project"
    seat_root = tmp_path / "seats" / "worker-a"
    credential = tmp_path / "worker.env"
    repository.mkdir(parents=True)
    seat_root.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    record = {
        "role": "worker",
        "principal_id": "PR-worker-a",
        "credential_ref": "credential.worker-a",
        "repository_root": str(repository),
        "seat_root": str(seat_root),
        "command": [sys.executable, "-c", "raise SystemExit(0)"],
        "boards": "registry",
        "capabilities": {
            "can_work": True,
            "can_review": False,
            "tier_max": 2,
            "max_parallel": 1,
        },
    }
    template = fleet_executor.SeatTemplate.from_record("template:worker:direct", record)
    private = Ed25519PrivateKey.generate()
    executor_policy = fleet_executor.ExecutorPolicy(
        authorization_fingerprint_sha256=FINGERPRINT,
        templates={template.template_id: template},
        caller_keys={"butler-local": private.public_key()},
        credential_paths={"credential.worker-a": credential},
        repository_roots=(repository.parent.resolve(),),
        seat_roots=(seat_root.parent.resolve(),),
        board_caps={"pursers": 2},
        host_cap=2,
        signature_skew_s=90,
        mutation_cooldown_s=0,
        failure_backoff_s=0,
    )
    adapter = FakeServiceAdapter()
    private_path = (tmp_path / "butler.key").resolve()
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    socket_root = Path.home() / ".cache" / "pursers" / "worker-10" / "test-sockets"
    socket_root.mkdir(parents=True, exist_ok=True)
    socket_path = (socket_root / f"executor-{os.getpid()}.sock").resolve()
    socket_path.unlink(missing_ok=True)
    ready = threading.Event()

    def serve_once() -> None:
        service = fleet_executor.FleetExecutor(
            executor_policy,
            fleet_executor.ExecutorStore(tmp_path / "state" / "executor.sqlite3"),
            adapter,
            KnownLease(),
            ReadyRegistry(),
            ReceiptSink(),
            clock=lambda: NOW.timestamp(),
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(os.fspath(socket_path))
            socket_path.chmod(0o600)
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                payload = bytearray()
                while not payload.endswith(b"\n"):
                    payload.extend(connection.recv(4096))
                result = service.handle(json.loads(payload))
                connection.sendall(fleet_executor.canonical_json(result) + b"\n")

    server = threading.Thread(target=serve_once, daemon=True)
    server.start()
    assert ready.wait(timeout=5)
    current = snapshot(
        {"pursers": demand(work=1)},
        [
            seat(
                "worker-a",
                "worker",
                template_id=template.template_id,
                template_digest=template.digest_sha256,
            )
        ],
    )

    report = reconciler().reconcile(
        current,
        butler.MemoryFleetStateStore(),
        butler.UnixFleetExecutorClient(
            socket_path,
            "butler-local",
            private_path,
            clock=lambda: NOW,
        ),
    )
    server.join(timeout=5)
    socket_path.unlink(missing_ok=True)

    assert report["receipts"][0]["outcome"] == "succeeded"
    assert report["receipts"][0]["committed"] is True
    assert adapter.observation.ready is True


def test_production_fleet_cycle_reads_products_executes_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle_now = datetime.now(timezone.utc)
    repository = tmp_path / "repos" / "project"
    seat_root = tmp_path / "seats" / "worker-a"
    credential = tmp_path / "worker.env"
    repository.mkdir(parents=True)
    seat_root.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    record = {
        "role": "worker",
        "principal_id": "PR-worker-a",
        "credential_ref": "credential.worker-a",
        "repository_root": str(repository),
        "seat_root": str(seat_root),
        "command": [sys.executable, "-c", "raise SystemExit(0)"],
        "boards": "registry",
        "capabilities": {
            "can_work": True,
            "can_review": False,
            "tier_max": 2,
            "max_parallel": 1,
        },
    }
    template = fleet_executor.SeatTemplate.from_record(
        "template:worker:direct", record
    )
    private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "butler.key").resolve()
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    executor_policy = fleet_executor.ExecutorPolicy(
        authorization_fingerprint_sha256=FINGERPRINT,
        templates={template.template_id: template},
        caller_keys={"butler-local": private.public_key()},
        credential_paths={"credential.worker-a": credential},
        repository_roots=(repository.parent.resolve(),),
        seat_roots=(seat_root.parent.resolve(),),
        board_caps={"pursers": 2},
        host_cap=2,
        signature_skew_s=90,
        mutation_cooldown_s=0,
        failure_backoff_s=0,
    )
    adapter = FakeServiceAdapter()
    socket_root = Path.home() / ".cache" / "pursers" / "worker-10" / "test-sockets"
    socket_root.mkdir(parents=True, exist_ok=True)
    socket_path = (socket_root / f"cycle-{os.getpid()}.sock").resolve()
    socket_path.unlink(missing_ok=True)
    ready = threading.Event()

    def serve_once() -> None:
        service = fleet_executor.FleetExecutor(
            executor_policy,
            fleet_executor.ExecutorStore(tmp_path / "executor" / "executor.sqlite3"),
            adapter,
            KnownLease(),
            ReadyRegistry(),
            ReceiptSink(),
            clock=lambda: cycle_now.timestamp(),
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(os.fspath(socket_path))
            socket_path.chmod(0o600)
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                payload = bytearray()
                while not payload.endswith(b"\n"):
                    payload.extend(connection.recv(4096))
                result = service.handle(json.loads(payload))
                connection.sendall(fleet_executor.canonical_json(result) + b"\n")

    server = threading.Thread(target=serve_once, daemon=True)
    server.start()
    assert ready.wait(timeout=5)
    observation_path = (tmp_path / "fleet-observation.json").resolve()
    observation_path.write_text(
        json.dumps(
            {
                "schema": "pursers_fleet_observation_v1",
                "schema_version": 1,
                "observed_at": (cycle_now - timedelta(seconds=1)).isoformat(),
                "stale_after": (cycle_now + timedelta(minutes=1)).isoformat(),
                "executor_seats": [
                    {
                        "seat_id": "worker-a",
                        "board_id": "pursers",
                        "role": "worker",
                        "provider": "direct",
                        "template_id": template.template_id,
                        "template_digest_sha256": template.digest_sha256,
                        "generation": 1,
                        "lifecycle": "stopped",
                        "transition_at": (
                            cycle_now - timedelta(minutes=10)
                        ).isoformat(),
                        "managed": True,
                    }
                ],
                "provider_observations": {
                    "pursers": {
                        "direct": {"status": "healthy", "latency_ms": 5}
                    }
                },
                "provider_maximums": {"pursers": {"direct": 2}},
                "host_observation": {
                    "load_ratio": 0.1,
                    "capacity_available": True,
                    "executor_status": "healthy",
                },
            }
        ),
        encoding="utf-8",
    )
    observation_path.chmod(0o600)
    options = SimpleNamespace(
        url="https://central.invalid/mcp",
        home_board="pursers",
        agent_name="board-butler-test",
        runtime_mode="active",
        fleet_observation_file=observation_path,
        fleet_state_file=(tmp_path / "fleet-state.json").resolve(),
        fleet_executor_socket=socket_path,
        fleet_executor_key_id="butler-local",
        fleet_executor_private_key=private_path,
    )
    backend = butler.CentralBackend(options, "opaque")
    config = active_config()
    config["authorization"]["expires_at"] = (
        cycle_now + timedelta(hours=1)
    ).isoformat()
    published: dict[str, Mapping[str, Any]] = {}

    async def configs(_board_ids: Any) -> dict[str, Mapping[str, Any]]:
        return {"pursers": config}

    async def publish(board_id: str, document: Mapping[str, Any]) -> None:
        published[board_id] = dict(document)

    monkeypatch.setattr(backend, "_autonomous_fleet_configs", configs)
    monkeypatch.setattr(backend, "_write_fleet_state", publish)
    board_snapshot = {
        "truncated": False,
        "tickets": [
            {
                "ticket_id": "TK-burst",
                "status": "open",
                "tier": 1,
                "tags": [],
                "created_at": (
                    cycle_now - timedelta(minutes=10)
                ).isoformat(),
            }
        ],
        "agents": [],
    }

    result = asyncio.run(
        backend._reconcile_fleet(
            ["pursers"], {"pursers": board_snapshot}, cycle_now
        )
    )
    server.join(timeout=5)
    socket_path.unlink(missing_ok=True)

    assert result == {
        "status": "reconciled",
        "boards": ["pursers"],
        "operations": 1,
        "receipt_outcomes": ["succeeded"],
    }
    assert adapter.observation.ready is True
    assert published["pursers"]["schema"] == "autonomous_butler_state_v1"
    assert json.loads(options.fleet_state_file.read_text())["revision"] == 2

    second_now = cycle_now + timedelta(seconds=1)
    new_fingerprint = "c" * 64
    config["revision"] = 4
    config["authorization"]["config_revision"] = 4
    config["authorization"]["envelope_fingerprint_sha256"] = new_fingerprint
    config["envelope"]["fingerprint_sha256"] = new_fingerprint
    config["host_runtime"]["agent_process_ceiling"] = 1
    config["host_runtime"]["total_process_ceiling"] = 3
    config["desired"]["host_concurrency"] = 1
    config["desired"]["board_concurrency"] = 1
    config["envelope"]["max_host_concurrency"] = 1
    config["envelope"]["max_board_concurrency"] = 1
    for role in ("reviewer", "acp_worker"):
        config["desired"]["capacity"][role] = {"min": 0, "target": 0, "max": 0}
        config["envelope"]["max_capacity"][role] = 0
    config["desired"]["capacity"]["worker"]["max"] = 1
    config["envelope"]["max_capacity"]["worker"] = 1
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["observed_at"] = (
        second_now - timedelta(milliseconds=100)
    ).isoformat()
    observation["stale_after"] = (second_now + timedelta(minutes=1)).isoformat()
    observation["provider_maximums"]["pursers"]["direct"] = 1
    observation["executor_seats"][0]["lifecycle"] = "ready"
    observation["executor_seats"][0]["transition_at"] = cycle_now.isoformat()
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    observation_path.chmod(0o600)

    changed = asyncio.run(
        backend._reconcile_fleet(
            ["pursers"], {"pursers": board_snapshot}, second_now
        )
    )

    assert changed == {
        "status": "reconciled",
        "boards": ["pursers"],
        "operations": 0,
        "receipt_outcomes": [],
    }
    assert published["pursers"]["config_revision"] == 4
    assert published["pursers"]["capacity"]["worker"]["desired"] == 1
    durable = json.loads(options.fleet_state_file.read_text())
    assert durable["revision"] == 4
    assert durable["value"]["config_revisions"] == {"pursers": 4}
    assert durable["value"]["policy_transition"][
        "discarded_nonterminal_operations"
    ] == 0

    stale_now = second_now + timedelta(seconds=1)
    config["revision"] = 3
    config["authorization"]["config_revision"] = 3
    config["authorization"]["envelope_fingerprint_sha256"] = FINGERPRINT
    config["envelope"]["fingerprint_sha256"] = FINGERPRINT
    config["host_runtime"]["agent_process_ceiling"] = 2
    config["host_runtime"]["total_process_ceiling"] = 4
    config["desired"]["host_concurrency"] = 2
    config["desired"]["board_concurrency"] = 2
    config["envelope"]["max_host_concurrency"] = 2
    config["envelope"]["max_board_concurrency"] = 2
    config["desired"]["capacity"]["worker"]["max"] = 2
    config["envelope"]["max_capacity"]["worker"] = 2
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["observed_at"] = (
        stale_now - timedelta(milliseconds=100)
    ).isoformat()
    observation["stale_after"] = (
        stale_now + timedelta(minutes=1)
    ).isoformat()
    observation["provider_maximums"]["pursers"]["direct"] = 2
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    observation_path.chmod(0o600)
    stale_backend = butler.CentralBackend(options, "opaque")
    stale_publications: list[tuple[str, Mapping[str, Any]]] = []

    async def reject_publish(
        board_id: str, document: Mapping[str, Any]
    ) -> None:
        stale_publications.append((board_id, dict(document)))

    monkeypatch.setattr(stale_backend, "_autonomous_fleet_configs", configs)
    monkeypatch.setattr(stale_backend, "_write_fleet_state", reject_publish)

    stale = asyncio.run(
        stale_backend._reconcile_fleet(
            ["pursers"], {"pursers": board_snapshot}, stale_now
        )
    )

    assert stale["status"] == "shadow"
    assert stale["effective_state"] == "shadow"
    assert stale["reason_code"] == "stale_config_revision"
    assert stale["audit_evidence"] == [
        {
            "board_id": "pursers",
            "action": "reconcile",
            "outcome": "denied",
            "reason_code": "stale_config_revision",
            "current_revision": 3,
            "durable_revision": 4,
            "observed_at": stale_now.isoformat(),
            "detail_redacted": True,
        }
    ]
    assert stale_publications == []
    assert json.loads(options.fleet_state_file.read_text()) == durable
