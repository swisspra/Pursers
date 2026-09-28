#!/usr/bin/env python3
"""Plan and confirm Butler-owned supervisor rosters without touching the host.

The Board Butler refresh path persists the confirmed document and the host
executor consumes it as an authorization boundary. This module deliberately
has no process, launchd, credential, or filesystem mutation surface.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol


ROSTER_SCHEMA = "pursers_supervisor_roster_v1"
ROSTER_STATE_KEY = "supervisor_roster"
PLAN_SCHEMA = "pursers_supervisor_roster_plan_v1"
MAX_OBSERVATION_AGE_S = 180
PLAN_FIELDS = frozenset(
    {
        "schema", "board_id", "prior_revision", "next_revision",
        "config_revision", "envelope_fingerprint_sha256", "host_seat_cap",
        "gate_concurrency_ceiling",
        "observation_digest_sha256", "desired", "full_gate_concurrency",
        "project_admission",
        "actions", "findings", "audit", "plan_digest_sha256",
    }
)
ROLES = ("worker", "reviewer", "verifier")
LIFECYCLES = frozenset({"ready", "busy", "draining", "paused", "stopped"})
MUTATING_ACTIONS = frozenset(
    {"provision", "start", "resume", "drain", "pause", "stop", "remove", "re_role"}
)
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


class SupervisorPlanError(ValueError):
    """One bounded fail-closed roster planning error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise SupervisorPlanError(f"{field}_invalid")
    return value


def _count(value: Any, field: str, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise SupervisorPlanError(f"{field}_invalid")
    return value


def _ratio(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise SupervisorPlanError(f"{field}_invalid")
    return float(value)


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise SupervisorPlanError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisorPlanError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise SupervisorPlanError(f"{field}_invalid")
    return parsed


@dataclass(frozen=True)
class SupervisorGrant:
    board_id: str
    config_revision: int
    envelope_fingerprint_sha256: str
    host_seat_cap: int
    gate_concurrency_ceiling: int
    approved_template_ids: tuple[str, ...]
    worker_floor: int = 1
    reviewer_floor: int = 1
    verifier_floor: int = 0
    cooldown_s: int = 120

    def __post_init__(self) -> None:
        _identifier(self.board_id, "board_id")
        _count(self.config_revision, "config_revision", 2**63 - 1)
        if self.config_revision < 1 or SHA256.fullmatch(self.envelope_fingerprint_sha256) is None:
            raise SupervisorPlanError("authorization_invalid")
        if not 1 <= self.host_seat_cap <= 100:
            raise SupervisorPlanError("host_seat_cap_invalid")
        if not 1 <= self.gate_concurrency_ceiling <= 100:
            raise SupervisorPlanError("gate_concurrency_ceiling_invalid")
        if (
            not self.approved_template_ids
            or len(self.approved_template_ids) != len(set(self.approved_template_ids))
        ):
            raise SupervisorPlanError("approved_templates_invalid")
        for template_id in self.approved_template_ids:
            _identifier(template_id, "template_id")
        floors = (self.worker_floor, self.reviewer_floor, self.verifier_floor)
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in floors):
            raise SupervisorPlanError("role_floor_invalid")
        if sum(floors) > self.host_seat_cap or not 0 <= self.cooldown_s <= 86_400:
            raise SupervisorPlanError("role_floor_exceeds_cap")


@dataclass(frozen=True)
class SupervisorSeat:
    seat_id: str
    identity_id: str
    state_id: str
    state_dir_id: str
    generation: int
    role: str
    lifecycle: str
    work_claim: bool
    review_lease: bool
    transition_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.seat_id, "seat_id")
        _identifier(self.identity_id, "identity_id")
        _identifier(self.state_id, "state_id")
        _identifier(self.state_dir_id, "state_dir_id")
        if self.generation < 1:
            raise SupervisorPlanError("seat_generation_invalid")
        if self.role not in ROLES or self.lifecycle not in LIFECYCLES:
            raise SupervisorPlanError("seat_state_invalid")
        if self.transition_at.tzinfo is None:
            raise SupervisorPlanError("seat_transition_invalid")

    @property
    def active(self) -> bool:
        return self.lifecycle in {"ready", "busy", "draining"}

    @property
    def leased(self) -> bool:
        return self.work_claim or self.review_lease


@dataclass(frozen=True)
class SupervisorDemand:
    unassignable_work: int
    unassignable_review: int
    oldest_work_age_s: int
    oldest_review_age_s: int
    rejection_rework: int
    full_gate_queue_depth: int
    full_gate_oldest_wait_s: int

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            _count(value, field)


@dataclass(frozen=True)
class SupervisorProjectDemand:
    project_id: str
    priority: int
    demand: SupervisorDemand

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        if not 1 <= self.priority <= 100:
            raise SupervisorPlanError("project_priority_invalid")


@dataclass(frozen=True)
class SupervisorObservation:
    observed_at: datetime
    seats: tuple[SupervisorSeat, ...]
    demand: SupervisorDemand
    host_load_ratio: float
    memory_headroom_ratio: float
    disk_headroom_ratio: float
    projects: tuple[SupervisorProjectDemand, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise SupervisorPlanError("observation_time_invalid")
        _ratio(self.host_load_ratio, "host_load_ratio")
        _ratio(self.memory_headroom_ratio, "memory_headroom_ratio")
        _ratio(self.disk_headroom_ratio, "disk_headroom_ratio")
        for attribute in ("seat_id", "identity_id", "state_id", "state_dir_id"):
            values = [getattr(seat, attribute) for seat in self.seats]
            if len(values) != len(set(values)):
                raise SupervisorPlanError(f"ambiguous_{attribute}")
        project_ids = [project.project_id for project in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise SupervisorPlanError("ambiguous_project_id")
        if self.projects and self.demand != _aggregate_project_demand(self.projects):
            raise SupervisorPlanError("project_demand_aggregate_mismatch")


def _validate_config_schema(config: Mapping[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator

        schema_path = (
            Path(__file__).resolve().parents[2]
            / "docs/design/schemas/autonomous-butler-config-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(dict(config))
    except Exception as exc:
        raise SupervisorPlanError("config_schema_invalid") from exc


def grant_from_config(config: Mapping[str, Any], now: datetime) -> SupervisorGrant:
    """Resolve the one-number grant from the existing authorized config."""
    if now.tzinfo is None or not isinstance(config, Mapping):
        raise SupervisorPlanError("config_invalid")
    _validate_config_schema(config)
    desired = config.get("desired")
    envelope = config.get("envelope")
    authorization = config.get("authorization")
    host = config.get("host_runtime")
    if (
        config.get("schema") != "autonomous_butler_config_v1"
        or config.get("enabled") is not True
        or not all(isinstance(item, Mapping) for item in (desired, envelope, authorization, host))
        or desired.get("mode") != "autonomous"
    ):
        raise SupervisorPlanError("config_not_authorized")
    revision = config.get("revision")
    fingerprint = envelope.get("fingerprint_sha256")
    expires_at = _time(authorization.get("expires_at"), "authorization_expiry")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(fingerprint, str)
        or SHA256.fullmatch(fingerprint) is None
        or authorization.get("config_revision") != revision
        or authorization.get("envelope_fingerprint_sha256") != fingerprint
        or expires_at <= now
    ):
        raise SupervisorPlanError("authorization_invalid")
    cap = envelope.get("host_seat_cap", host.get("agent_process_ceiling"))
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise SupervisorPlanError("host_seat_cap_invalid")
    if cap != host.get("agent_process_ceiling"):
        raise SupervisorPlanError("host_seat_cap_invalid")
    cooldowns = desired.get("cooldowns")
    cooldown = 120
    if isinstance(cooldowns, Mapping):
        raw = cooldowns.get("scale_down_s", cooldown)
        if isinstance(raw, int) and not isinstance(raw, bool):
            cooldown = raw
    return SupervisorGrant(
        board_id=_identifier(config.get("board_id"), "board_id"),
        config_revision=revision,
        envelope_fingerprint_sha256=fingerprint,
        host_seat_cap=cap,
        gate_concurrency_ceiling=int(envelope["max_host_concurrency"]),
        approved_template_ids=tuple(sorted(envelope["approved_template_ids"])),
        cooldown_s=cooldown,
    )


def _demand_from_mapping(value: Mapping[str, Any]) -> SupervisorDemand:
    expected = {
        "unassignable_work", "unassignable_review", "oldest_work_age_s",
        "oldest_review_age_s", "rejection_rework", "full_gate_queue_depth",
        "full_gate_oldest_wait_s",
    }
    if set(value) != expected:
        raise SupervisorPlanError("observation_fields_invalid")
    return SupervisorDemand(**{key: value[key] for key in expected})


def _aggregate_project_demand(
    projects: tuple[SupervisorProjectDemand, ...],
) -> SupervisorDemand:
    return SupervisorDemand(
        unassignable_work=sum(item.demand.unassignable_work for item in projects),
        unassignable_review=sum(item.demand.unassignable_review for item in projects),
        oldest_work_age_s=max((item.demand.oldest_work_age_s for item in projects), default=0),
        oldest_review_age_s=max((item.demand.oldest_review_age_s for item in projects), default=0),
        rejection_rework=sum(item.demand.rejection_rework for item in projects),
        full_gate_queue_depth=sum(item.demand.full_gate_queue_depth for item in projects),
        full_gate_oldest_wait_s=max(
            (item.demand.full_gate_oldest_wait_s for item in projects), default=0
        ),
    )


def observation_from_fixture(value: Mapping[str, Any]) -> SupervisorObservation:
    required = {"observed_at", "seats", "demand", "host"}
    if (
        not isinstance(value, Mapping)
        or not required <= set(value)
        or set(value) - required - {"projects"}
    ):
        raise SupervisorPlanError("observation_fields_invalid")
    demand = value["demand"]
    host = value["host"]
    seats = value["seats"]
    if (
        not isinstance(demand, Mapping)
        or not isinstance(host, Mapping)
        or not isinstance(seats, list)
    ):
        raise SupervisorPlanError("observation_invalid")
    if set(host) != {
        "load_ratio", "memory_headroom_ratio", "disk_headroom_ratio"
    }:
        raise SupervisorPlanError("observation_fields_invalid")
    parsed_projects: list[SupervisorProjectDemand] = []
    for row in value.get("projects", []):
        if not isinstance(row, Mapping) or set(row) != {"project_id", "priority", "demand"}:
            raise SupervisorPlanError("project_demand_fields_invalid")
        if not isinstance(row["demand"], Mapping):
            raise SupervisorPlanError("project_demand_invalid")
        parsed_projects.append(
            SupervisorProjectDemand(
                project_id=_identifier(row["project_id"], "project_id"),
                priority=_count(row["priority"], "project_priority", 100),
                demand=_demand_from_mapping(row["demand"]),
            )
        )
    parsed_seats: list[SupervisorSeat] = []
    for row in seats:
        if not isinstance(row, Mapping) or set(row) != {
            "seat_id", "identity_id", "state_id", "state_dir_id", "generation",
            "role", "lifecycle",
            "work_claim", "review_lease", "transition_at",
        }:
            raise SupervisorPlanError("seat_fields_invalid")
        if not isinstance(row["work_claim"], bool) or not isinstance(row["review_lease"], bool):
            raise SupervisorPlanError("seat_lease_invalid")
        parsed_seats.append(
            SupervisorSeat(
                seat_id=row["seat_id"], identity_id=row["identity_id"],
                state_id=row["state_id"], state_dir_id=row["state_dir_id"],
                generation=_count(row["generation"], "seat_generation"),
                role=row["role"], lifecycle=row["lifecycle"],
                work_claim=row["work_claim"], review_lease=row["review_lease"],
                transition_at=_time(row["transition_at"], "seat_transition"),
            )
        )
    return SupervisorObservation(
        observed_at=_time(value["observed_at"], "observed_at"),
        seats=tuple(parsed_seats),
        demand=_demand_from_mapping(demand),
        host_load_ratio=_ratio(host["load_ratio"], "host_load_ratio"),
        memory_headroom_ratio=_ratio(host["memory_headroom_ratio"], "memory_headroom_ratio"),
        disk_headroom_ratio=_ratio(host["disk_headroom_ratio"], "disk_headroom_ratio"),
        projects=tuple(parsed_projects),
    )


def _observation_document(observation: SupervisorObservation) -> dict[str, Any]:
    return {
        "observed_at": observation.observed_at.isoformat(),
        "seats": [
            {
                "seat_id": seat.seat_id, "identity_id": seat.identity_id,
                "state_id": seat.state_id, "state_dir_id": seat.state_dir_id,
                "generation": seat.generation, "role": seat.role,
                "lifecycle": seat.lifecycle, "work_claim": seat.work_claim,
                "review_lease": seat.review_lease,
                "transition_at": seat.transition_at.isoformat(),
            }
            for seat in sorted(observation.seats, key=lambda item: item.seat_id)
        ],
        "demand": dict(observation.demand.__dict__),
        "host": {
            "load_ratio": observation.host_load_ratio,
            "memory_headroom_ratio": observation.memory_headroom_ratio,
            "disk_headroom_ratio": observation.disk_headroom_ratio,
        },
        "projects": [
            {
                "project_id": project.project_id,
                "priority": project.priority,
                "demand": dict(project.demand.__dict__),
            }
            for project in sorted(observation.projects, key=lambda item: item.project_id)
        ],
    }


def _project_admission(
    grant: SupervisorGrant, observation: SupervisorObservation
) -> list[dict[str, Any]]:
    projects = observation.projects or (
        SupervisorProjectDemand(grant.board_id, 50, observation.demand),
    )
    rows: list[dict[str, Any]] = []
    for project in sorted(projects, key=lambda item: item.project_id):
        demand = project.demand
        role_pressure = {
            "worker": demand.unassignable_work + demand.rejection_rework,
            "reviewer": demand.unassignable_review + demand.rejection_rework,
            "verifier": demand.full_gate_queue_depth,
        }
        total = sum(role_pressure.values())
        age_boost = max(
            demand.oldest_work_age_s,
            demand.oldest_review_age_s,
            demand.full_gate_oldest_wait_s,
        ) // 120
        rows.append(
            {
                "project_id": project.project_id,
                "priority": project.priority,
                "share_units": max(1, project.priority + total + age_boost) if total else 0,
                "role_pressure": role_pressure,
            }
        )
    return rows


def _desired_mix(
    grant: SupervisorGrant, observation: SupervisorObservation
) -> tuple[dict[str, int], int]:
    demand = observation.demand
    scores = {
        "worker": demand.unassignable_work * 3 + demand.rejection_rework * 2,
        "reviewer": demand.unassignable_review * 3 + demand.rejection_rework,
        "verifier": demand.full_gate_queue_depth * 2,
    }
    if demand.oldest_work_age_s >= 120:
        scores["worker"] += 2
    if demand.oldest_review_age_s >= 120:
        scores["reviewer"] += 2
    if demand.full_gate_oldest_wait_s >= 120:
        scores["verifier"] += 2
    desired = {
        "worker": grant.worker_floor,
        "reviewer": grant.reviewer_floor,
        "verifier": grant.verifier_floor,
    }
    capacity = grant.host_seat_cap - sum(desired.values())
    while capacity and any(scores.values()):
        role = max(
            ROLES,
            key=lambda item: (
                scores[item] / (desired[item] + 1),
                scores[item],
                -ROLES.index(item),
            ),
        )
        desired[role] += 1
        scores[role] = max(0, scores[role] - 2)
        capacity -= 1
    headroom = min(observation.memory_headroom_ratio, observation.disk_headroom_ratio)
    healthy = observation.host_load_ratio <= 0.75 and headroom >= 0.35
    if demand.full_gate_queue_depth == 0:
        gate_budget = 0
    elif healthy:
        gate_budget = min(
            grant.gate_concurrency_ceiling,
            max(1, math.ceil(demand.full_gate_queue_depth / 2)),
        )
    else:
        gate_budget = 1
        # A saturated host preserves the role floors and releases gate-heavy
        # capacity instead of admitting more simultaneous full gates.
        desired["verifier"] = grant.verifier_floor
    return desired, gate_budget


def _template_for_role(grant: SupervisorGrant, role: str) -> str:
    aliases = (role, "acp_worker" if role == "verifier" else role)
    matches = [
        template_id
        for template_id in grant.approved_template_ids
        if any(alias in template_id.split(":") for alias in aliases)
    ]
    if len(matches) != 1:
        raise SupervisorPlanError(f"approved_template_for_{role}_invalid")
    return matches[0]


def _provision_action(
    grant: SupervisorGrant, role: str, prior_revision: int, ordinal: int
) -> dict[str, Any]:
    material = digest(
        {
            "board_id": grant.board_id,
            "config_revision": grant.config_revision,
            "envelope": grant.envelope_fingerprint_sha256,
            "prior_revision": prior_revision,
            "role": role,
            "ordinal": ordinal,
        }
    )
    return {
        "kind": "provision",
        "target_role": role,
        "template_id": _template_for_role(grant, role),
        "seat_id": f"seat:{material[:32]}",
        "identity_id": f"identity:{material[8:40]}",
        "state_id": f"state:{material[16:48]}",
        "state_dir_id": f"state-dir:{material[24:56]}",
        "generation": 1,
    }


def _action(
    kind: str,
    seat: SupervisorSeat | None,
    *,
    target_role: str | None = None,
    template_id: str | None = None,
) -> dict[str, Any]:
    if kind not in MUTATING_ACTIONS:
        raise SupervisorPlanError("action_invalid")
    row: dict[str, Any] = {"kind": kind}
    if seat is not None:
        row.update(
            {
                "seat_id": seat.seat_id,
                "identity_id": seat.identity_id,
                "state_id": seat.state_id,
                "state_dir_id": seat.state_dir_id,
                "generation": seat.generation,
            }
        )
    if target_role is not None:
        if target_role not in ROLES:
            raise SupervisorPlanError("target_role_invalid")
        row["target_role"] = target_role
    if template_id is not None:
        row["template_id"] = _identifier(template_id, "template_id")
    return row


def create_plan(
    grant: SupervisorGrant,
    observation: SupervisorObservation,
    *,
    prior_revision: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one deterministic plan. It cannot mutate a process or roster."""
    _count(prior_revision, "prior_revision", 2**63 - 1)
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise SupervisorPlanError("current_time_invalid")
    age_s = (current - observation.observed_at).total_seconds()
    if age_s < -5 or age_s > MAX_OBSERVATION_AGE_S:
        raise SupervisorPlanError("observation_stale")
    desired, gate_budget = _desired_mix(grant, observation)
    active = [seat for seat in observation.seats if seat.active]
    actions: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if len(observation.seats) > grant.host_seat_cap:
        findings.append(
            {
                "reason_code": "observed_seats_exceed_cap",
                "observed": len(observation.seats),
                "host_seat_cap": grant.host_seat_cap,
            }
        )
    now = observation.observed_at
    selected: set[str] = set()
    deficits: dict[str, int] = {}
    surpluses: list[SupervisorSeat] = []
    for role in ROLES:
        role_active = sorted(
            (seat for seat in active if seat.role == role),
            key=lambda seat: (seat.leased, seat.lifecycle == "busy", seat.seat_id),
            reverse=True,
        )
        keep = role_active[: desired[role]]
        selected.update(seat.seat_id for seat in keep)
        deficits[role] = max(0, desired[role] - len(keep))
        surpluses.extend(role_active[desired[role] :])

    for target_role in sorted(ROLES, key=lambda role: (-deficits[role], ROLES.index(role))):
        while deficits[target_role] and surpluses:
            seat = sorted(surpluses, key=lambda item: (item.leased, item.seat_id))[0]
            surpluses.remove(seat)
            cooled = (now - seat.transition_at).total_seconds() >= grant.cooldown_s
            if seat.lifecycle == "draining" and not seat.leased and cooled:
                actions.append(
                    _action(
                        "re_role",
                        seat,
                        target_role=target_role,
                        template_id=_template_for_role(grant, target_role),
                    )
                )
                deficits[target_role] -= 1
            elif seat.lifecycle != "draining" and cooled:
                actions.append(_action("drain", seat, target_role=target_role))

    active_count = len(active)
    for role in ROLES:
        paused = sorted(
            (
                seat
                for seat in observation.seats
                if seat.role == role and seat.lifecycle in {"paused", "stopped"}
            ),
            key=lambda seat: seat.seat_id,
        )
        while deficits[role] and paused and active_count < grant.host_seat_cap:
            seat = paused.pop(0)
            if (now - seat.transition_at).total_seconds() < grant.cooldown_s:
                break
            actions.append(_action("resume" if seat.lifecycle == "paused" else "start", seat))
            deficits[role] -= 1
            active_count += 1
        while deficits[role] and len(observation.seats) + sum(
            action["kind"] == "provision" for action in actions
        ) < grant.host_seat_cap:
            actions.append(
                _provision_action(grant, role, prior_revision, len(actions))
            )
            deficits[role] -= 1
            active_count += 1

    for seat in sorted(surpluses, key=lambda item: (item.leased, item.seat_id)):
        if (now - seat.transition_at).total_seconds() < grant.cooldown_s:
            continue
        if seat.lifecycle == "draining" and not seat.leased:
            actions.append(_action("pause", seat))
        elif seat.lifecycle != "draining":
            actions.append(_action("drain", seat))

    body = {
        "schema": PLAN_SCHEMA,
        "board_id": grant.board_id,
        "prior_revision": prior_revision,
        "next_revision": prior_revision + 1,
        "config_revision": grant.config_revision,
        "envelope_fingerprint_sha256": grant.envelope_fingerprint_sha256,
        "host_seat_cap": grant.host_seat_cap,
        "gate_concurrency_ceiling": grant.gate_concurrency_ceiling,
        "observation_digest_sha256": digest(_observation_document(observation)),
        "desired": desired,
        "full_gate_concurrency": gate_budget,
        "project_admission": _project_admission(grant, observation),
        "actions": actions,
        "findings": findings,
        "audit": [
            {
                "action": action["kind"],
                "seat_id": action.get("seat_id"),
                "target_role": action.get("target_role"),
                "reason_code": "demand_reconciliation",
            }
            for action in actions
        ],
    }
    body["plan_digest_sha256"] = digest(body)
    return body


def confirm_plan(
    plan: Mapping[str, Any],
    grant: SupervisorGrant,
    observation: SupervisorObservation,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Revalidate a plan and return the supervisor-consumable roster document."""
    current_time = datetime.now(timezone.utc) if now is None else now
    if current_time.tzinfo is None:
        raise SupervisorPlanError("current_time_invalid")
    if (
        not isinstance(plan, Mapping)
        or set(plan) != PLAN_FIELDS
        or plan.get("schema") != PLAN_SCHEMA
    ):
        raise SupervisorPlanError("plan_invalid")
    material = dict(plan)
    claimed_digest = material.pop("plan_digest_sha256", None)
    if not isinstance(claimed_digest, str) or not secrets_compare(claimed_digest, digest(material)):
        raise SupervisorPlanError("plan_digest_mismatch")
    if (
        plan.get("board_id") != grant.board_id
        or plan.get("config_revision") != grant.config_revision
        or plan.get("envelope_fingerprint_sha256") != grant.envelope_fingerprint_sha256
        or plan.get("host_seat_cap") != grant.host_seat_cap
        or plan.get("gate_concurrency_ceiling") != grant.gate_concurrency_ceiling
    ):
        raise SupervisorPlanError("authorization_changed")
    if plan.get("observation_digest_sha256") != digest(_observation_document(observation)):
        raise SupervisorPlanError("observation_changed")
    expected = create_plan(
        grant,
        observation,
        prior_revision=plan.get("prior_revision"),
        now=current_time,
    )
    if dict(plan) != expected:
        raise SupervisorPlanError("plan_not_current")
    desired = plan.get("desired")
    actions = plan.get("actions")
    if (
        not isinstance(desired, Mapping)
        or set(desired) != set(ROLES)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in desired.values()
        )
        or sum(desired.values()) > grant.host_seat_cap
        or not isinstance(actions, list)
    ):
        raise SupervisorPlanError("plan_capacity_invalid")
    seats = {seat.seat_id: seat for seat in observation.seats}
    projected_active = sum(seat.active for seat in observation.seats)
    for action in actions:
        if not isinstance(action, Mapping) or action.get("kind") not in MUTATING_ACTIONS:
            raise SupervisorPlanError("action_invalid")
        expected_fields = {
            "kind", "seat_id", "identity_id", "state_id", "state_dir_id", "generation"
        }
        if action["kind"] == "provision":
            expected_fields.update({"target_role", "template_id"})
        if action["kind"] == "drain" and "target_role" in action:
            expected_fields.add("target_role")
        if action["kind"] == "re_role":
            expected_fields.update({"target_role", "template_id"})
        if set(action) != expected_fields:
            raise SupervisorPlanError("action_fields_invalid")
        if action["kind"] != "provision":
            seat = seats.get(action.get("seat_id"))
            if (
                seat is None
                or action.get("identity_id") != seat.identity_id
                or action.get("state_id") != seat.state_id
                or action.get("state_dir_id") != seat.state_dir_id
                or action.get("generation") != seat.generation
            ):
                raise SupervisorPlanError("seat_identity_changed")
            if action["kind"] == "re_role" and (
                action.get("template_id") not in grant.approved_template_ids
                or action.get("template_id")
                != _template_for_role(grant, str(action.get("target_role")))
            ):
                raise SupervisorPlanError("re_role_template_not_approved")
        else:
            if action.get("template_id") not in grant.approved_template_ids:
                raise SupervisorPlanError("provision_template_not_approved")
            identifiers = [
                action.get("seat_id"),
                action.get("identity_id"),
                action.get("state_id"),
                action.get("state_dir_id"),
            ]
            if any(not isinstance(value, str) for value in identifiers):
                raise SupervisorPlanError("provision_identity_invalid")
            if any(
                value in {
                    getattr(seat, attribute)
                    for seat in observation.seats
                }
                for value, attribute in zip(
                    identifiers,
                    ("seat_id", "identity_id", "state_id", "state_dir_id"),
                    strict=True,
                )
            ):
                raise SupervisorPlanError("provision_identity_reused")
        if action["kind"] in {"pause", "stop", "remove", "re_role"}:
            seat = seats[action["seat_id"]]
            if seat is None or seat.lifecycle != "draining" or seat.leased:
                raise SupervisorPlanError("drain_or_lease_precondition_failed")
        if action["kind"] in {"provision", "start", "resume"}:
            projected_active += 1
            if projected_active > grant.host_seat_cap:
                raise SupervisorPlanError("plan_capacity_invalid")
        elif action["kind"] in {"pause", "stop", "remove"}:
            projected_active -= 1
    return {
        "schema": ROSTER_SCHEMA,
        "revision": plan["next_revision"],
        "board_id": grant.board_id,
        "config_revision": grant.config_revision,
        "envelope_fingerprint_sha256": grant.envelope_fingerprint_sha256,
        "host_seat_cap": grant.host_seat_cap,
        "gate_concurrency_ceiling": grant.gate_concurrency_ceiling,
        "desired": dict(desired),
        "full_gate_concurrency": plan["full_gate_concurrency"],
        "project_admission": [dict(row) for row in plan["project_admission"]],
        "seats": _observation_document(observation)["seats"],
        "actions": [dict(action) for action in actions],
        "plan_digest_sha256": claimed_digest,
        "confirmed_at": current_time.isoformat(),
        "audit": [dict(row) for row in plan.get("audit", [])],
        "findings": [dict(row) for row in plan.get("findings", [])],
    }


def secrets_compare(left: str, right: str) -> bool:
    # Kept local so importing this planning module does not pull host tooling.
    import hmac

    return hmac.compare_digest(left, right)


class ButlerCommandClient(Protocol):
    async def board_state_get(self, key: str) -> Mapping[str, Any]: ...

    async def board_state_update(
        self, key: str, value: str, *, expected_sha256: str | None = None
    ) -> Mapping[str, Any]: ...

    async def butler_command_submit(
        self,
        request_id: str,
        project_id: str,
        sender_channel: str,
        intent: str,
        parameters: dict[str, Any],
        expected_config_revision: int,
        expires_at: str,
        *,
        priority: str = "normal",
    ) -> Mapping[str, Any]: ...

    async def butler_command_acknowledge(
        self, command_id: str, expected_revision: int,
        target_status: str, reason_code: str,
    ) -> Mapping[str, Any]: ...

    async def butler_config_set(
        self, mutation_id: str, sender_channel: str, config: dict[str, Any],
        expected_revision: int, *, authorization_command_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    async def butler_command_result(
        self, command_id: str, expected_revision: int, result: dict[str, Any],
    ) -> Mapping[str, Any]: ...


def plan_host_seat_cap_grant(
    current: Mapping[str, Any], command: Mapping[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Plan the exact immutable-envelope update delegated by a human command."""
    sender = command.get("sender")
    parameters = command.get("parameters")
    cap = parameters.get("host_seat_cap") if isinstance(parameters, Mapping) else None
    if (
        command.get("intent") != "set_host_seat_cap"
        or command.get("status") != "applying"
        or not isinstance(sender, Mapping)
        or sender.get("channel") != "human"
        or isinstance(cap, bool)
        or not isinstance(cap, int)
        or not 2 <= cap <= 100
        or command.get("expected_config_revision") != current.get("revision")
    ):
        raise SupervisorPlanError("host_seat_cap_command_invalid")
    updated = copy.deepcopy(dict(current))
    updated["revision"] = int(current["revision"]) + 1
    updated["enabled"] = True
    updated["desired"]["mode"] = "autonomous"
    updated["host_runtime"].update(
        {
            "revision": int(current["host_runtime"]["revision"]) + 1,
            "agent_process_ceiling": cap,
            "configured_by": str(sender.get("agent_id")),
            "configured_at": now.astimezone(timezone.utc).isoformat(),
        }
    )
    updated["envelope"]["host_seat_cap"] = cap
    envelope = dict(updated["envelope"])
    envelope.pop("fingerprint_sha256", None)
    updated["envelope"]["fingerprint_sha256"] = digest(envelope)
    updated["authorization"] = {
        "authorization_id": command["command_id"],
        "config_revision": updated["revision"],
        "envelope_fingerprint_sha256": updated["envelope"]["fingerprint_sha256"],
        "expires_at": command["expires_at"],
    }
    body = {
        "schema": "pursers_host_seat_cap_plan_v1",
        "command_id": command["command_id"],
        "command_digest_sha256": command["request_digest_sha256"],
        "prior_config_digest_sha256": digest(current),
        "planned_at": now.astimezone(timezone.utc).isoformat(),
        "config": updated,
    }
    body["plan_digest_sha256"] = digest(body)
    return body


def confirm_host_seat_cap_grant(
    plan: Mapping[str, Any], current: Mapping[str, Any], command: Mapping[str, Any]
) -> dict[str, Any]:
    """Fail closed if the human command or current config changed after planning."""
    material = dict(plan)
    claimed = material.pop("plan_digest_sha256", None)
    if not isinstance(claimed, str) or not secrets_compare(claimed, digest(material)):
        raise SupervisorPlanError("host_seat_cap_plan_digest_mismatch")
    try:
        planned_at = datetime.fromisoformat(str(plan["planned_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise SupervisorPlanError("host_seat_cap_plan_invalid") from exc
    expected = plan_host_seat_cap_grant(current, command, now=planned_at)
    if dict(plan) != expected:
        raise SupervisorPlanError("host_seat_cap_plan_not_current")
    return copy.deepcopy(dict(plan["config"]))


async def apply_host_seat_cap_command(
    client: ButlerCommandClient,
    command: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Advance, plan, confirm and commit one cap-only human command."""
    active = dict(command)
    for target in ("validating", "pending", "applying"):
        response = await client.butler_command_acknowledge(
            str(active["command_id"]), int(active["revision"]), target,
            f"host_cap_{target}",
        )
        active = dict(response["command"])
    plan = plan_host_seat_cap_grant(current, active, now=now)
    updated = confirm_host_seat_cap_grant(plan, current, active)
    await client.butler_config_set(
        mutation_id=f"host-cap-{active['command_id']}",
        sender_channel="a2a",
        config=updated,
        expected_revision=int(current["revision"]),
        authorization_command_id=str(active["command_id"]),
    )
    result = await client.butler_command_result(
        str(active["command_id"]), int(active["revision"]),
        {
            "outcome": "succeeded",
            "reason_code": "host_seat_cap_committed",
            "commit_state": "reached",
            "effect_observation_ref": f"butler-config-{updated['revision']}",
            "config_revision": updated["revision"],
        },
    )
    return updated, result


async def confirm_with_command(
    client: ButlerCommandClient,
    plan: Mapping[str, Any],
    grant: SupervisorGrant,
    observation: SupervisorObservation,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """CAS-persist the canonical roster, then reference it from a command."""
    current_time = datetime.now(timezone.utc) if now is None else now
    roster = confirm_plan(plan, grant, observation, now=current_time)
    plan_digest = roster["plan_digest_sha256"]
    try:
        current_state = await client.board_state_get(ROSTER_STATE_KEY)
    except Exception as exc:
        if "state key not found" not in str(exc).lower():
            raise
        prior_value = None
    else:
        state = current_state.get("state", {})
        prior_value = state.get("value") if isinstance(state, Mapping) else None
        if prior_value is not None and not isinstance(prior_value, str):
            raise SupervisorPlanError("roster_state_invalid")
    if prior_value is not None:
        try:
            prior = json.loads(prior_value)
        except json.JSONDecodeError as exc:
            raise SupervisorPlanError("roster_state_invalid") from exc
        if (
            not isinstance(prior, Mapping)
            or prior.get("schema") != ROSTER_SCHEMA
            or prior.get("revision") != plan["prior_revision"]
        ):
            raise SupervisorPlanError("roster_revision_changed")
    elif plan["prior_revision"] != 0:
        raise SupervisorPlanError("roster_revision_changed")
    encoded = canonical_json(roster).decode("utf-8")
    await client.board_state_update(
        ROSTER_STATE_KEY,
        encoded,
        expected_sha256=(
            hashlib.sha256(prior_value.encode("utf-8")).hexdigest()
            if prior_value is not None
            else None
        ),
    )
    response = await client.butler_command_submit(
        request_id=f"roster-{plan_digest[:32]}",
        project_id=grant.board_id,
        sender_channel="a2a",
        intent="set_desired_state",
        parameters={
            "desired_revision": roster["revision"],
            "desired_digest_sha256": digest(roster),
        },
        expected_config_revision=grant.config_revision,
        expires_at=(current_time + timedelta(minutes=5)).isoformat(),
        priority="high",
    )
    return roster, response


def roster_or_legacy(
    stored_value: str | None, legacy: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    """Supervisor read boundary: fallback only when canonical state is absent."""
    if stored_value is None:
        return "legacy", dict(legacy)
    try:
        document = json.loads(stored_value)
    except json.JSONDecodeError as exc:
        raise SupervisorPlanError("roster_state_invalid") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema") != ROSTER_SCHEMA
        or document.get("revision", 0) < 1
        or not isinstance(document.get("actions"), list)
        or not isinstance(document.get("plan_digest_sha256"), str)
    ):
        raise SupervisorPlanError("roster_state_invalid")
    return "canonical", dict(document)
