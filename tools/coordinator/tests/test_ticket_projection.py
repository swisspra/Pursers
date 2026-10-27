"""Read boundary regressions: count equality must not authorize partial fleet data.

Only AST-selected pure Central/Butler code runs; no service module is imported.
The oversized stub is an honest synthetic boundary with the literal field set
from Central ticket_list's byte-cap fallback, not a retained crash payload.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence

import pytest

ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location("projection_coordinator", ROOT / "tools/coordinator/coordinator.py")
assert SPEC and SPEC.loader
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)
NOW = datetime(2030, 1, 8, 12, tzinfo=timezone.utc)


def pure_module(path: Path, names: set[str]) -> ModuleType:
    """Extract actual dependency functions without importing startup code."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in selected} == names
    module = ModuleType("projection_" + path.stem)
    sys.modules[module.__name__] = module
    exec("from __future__ import annotations\nimport copy, re\nfrom dataclasses import dataclass, field\nfrom datetime import datetime, timedelta, timezone\nfrom typing import Any, Mapping, Sequence", module.__dict__)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def central() -> ModuleType:
    return pure_module(ROOT / "packages/central/src/pursers_central/central.py", {
        "project_ticket_read", "_ticket_assignment", "_history_count",
        "_latest_history_entry", "_dispatch_summary", "_project_ticket_activity",
    })


@pytest.fixture(scope="module")
def fleet() -> ModuleType:
    return pure_module(ROOT / "tools/board-butler/board_butler.py", {
        "registry_fleet_snapshots", "fleet_snapshot_from_products", "FleetDemand",
        "FleetSeat", "FleetSnapshot", "parse_time",
    })


def ticket(ticket_id: str = "TK-86bbd7252c979488ea2f", **changes: Any) -> dict:
    row = {
        "ticket_id": ticket_id, "title": "synthetic projection reproducer",
        "status": "open", "priority": "medium", "parked": False,
        "tier": 2, "tags": [], "created_at": (NOW - timedelta(seconds=600)).isoformat(),
        "updated_at": NOW.isoformat(), "dispatch_state": None,
        "description": "", "required_fields": [], "forbidden": [],
    }
    return {**row, **changes}


def offer(**changes: Any) -> dict:
    return {
        "ticket_id": "TK-86bbd7252c979488ea2f", "kind": "work",
        "agent_id": "AI-holder", "agent_name": "fixture-holder",
        "offered_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
        "expires_at_epoch": (NOW + timedelta(seconds=60)).timestamp(), "cycle": 0,
        **changes,
    }


def review_lease(**changes: Any) -> dict:
    return {
        "reviewer_agent_id": "AI-holder", "reviewer_agent_name": "fixture-holder",
        "reviewer_principal_id": "PR-holder", "claimed_at": NOW.isoformat(),
        "renewed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=900)).isoformat(),
        "expires_at_epoch": (NOW + timedelta(seconds=900)).timestamp(), "ttl_s": 900,
        **changes,
    }


def work_claim(**changes: Any) -> dict:
    return {
        "claimed_by_agent_id": "AI-holder", "claimed_by": "fixture-holder",
        "claimed_by_principal_id": "PR-holder", "claimed_at": NOW.isoformat(),
        "lease_expires_at": (NOW + timedelta(seconds=900)).isoformat(), "ttl_s": 900,
        **changes,
    }


@pytest.mark.parametrize("boundary", ["list", "detail"])
@pytest.mark.parametrize("key", ["dispatch_state", "current_offer", "work_offer", "review_offer"])
def test_offer_expiry_before_offered_is_hydrated_then_incomplete(central, key, boundary):
    bad = offer(kind="review" if key == "review_offer" else "work",
                offered_at=(NOW + timedelta(seconds=61)).isoformat())
    if key == "dispatch_state":
        bad = {"state": "offered", **bad}
    row = work(central, ticket(**{key: bad}))
    if key == "current_offer":
        # Central work projection omits this legacy consumer field; exercise
        # that optional boundary honestly rather than assume it is projected.
        row[key] = bad
    initial = row if boundary == "list" else stub(row)
    snapshot = read(Transport([initial], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is False


@pytest.mark.parametrize("boundary", ["list", "detail"])
@pytest.mark.parametrize("changes", [
    {"claimed_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"renewed_at": (NOW + timedelta(seconds=901)).isoformat()},
    {"ttl_s": 899},
])
def test_review_lease_temporal_contradictions_are_incomplete(central, changes, boundary):
    row = work(central, ticket(status="submitted", review_lease=review_lease(**changes)))
    initial = row if boundary == "list" else stub(row)
    snapshot = read(Transport([initial], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is False


@pytest.mark.parametrize("boundary", ["list", "detail"])
@pytest.mark.parametrize("rounding", [0.0, 0.0000002])
def test_valid_renewed_review_ttl_and_epoch_rounding_stay_complete(central, boundary, rounding):
    expiry = NOW + timedelta(seconds=900)
    lease = review_lease(claimed_at=(NOW - timedelta(seconds=600)).isoformat(),
                         expires_at_epoch=expiry.timestamp() + rounding)
    row = work(central, ticket(status="submitted", review_lease=lease))
    initial = row if boundary == "list" else stub(row)
    snapshot = read(Transport([initial], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is True


@pytest.mark.parametrize("boundary", ["list", "detail"])
def test_existing_expired_offer_with_valid_order_stays_complete(central, boundary):
    expiry = NOW - timedelta(seconds=1)
    expired = offer(offered_at=(NOW - timedelta(seconds=61)).isoformat(),
                    expires_at=expiry.isoformat(), expires_at_epoch=expiry.timestamp() + 0.0000002)
    row = work(central, ticket(work_offer=expired))
    initial = row if boundary == "list" else stub(row)
    snapshot = read(Transport([initial], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is True


def test_work_projection_does_not_invent_renewal_ttl_relation(central):
    row = work(central, ticket(status="claimed", **work_claim(ttl_s=899)))
    assert "claimed_at" not in row  # Central work view omits claim/renew times.
    snapshot = read(Transport([stub(row)], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is True


def work(central: ModuleType, row: dict) -> dict:
    return central.project_ticket_read(row, view="work", dispatch_history=[], include_dispatch_history=False)


def stub(row: dict) -> dict:
    keys = (
        "ticket_id", "title", "status", "priority", "parked", "assigned_to",
        "assigned_to_agent_id", "assigned_to_kind", "claimed_by",
        "claimed_by_agent_id", "updated_at", "archived",
    )
    return {**{key: copy.deepcopy(row[key]) for key in keys if key in row},
            "omitted_sections": ["description", "annotations", "submission_history", "review_history", "dispatch_history"],
            "detail_ref": "board://board-fe05/ticket/" + row["ticket_id"], "oversized_for_list": True}


def page(rows: list, include_closed: bool = False, **changes: Any) -> dict:
    return {
        "ok": True, "tickets": rows, "count": len(rows), "returned_count": len(rows),
        "total_matching": len(rows), "has_more": False, "next_cursor": None,
        "view": "work", "include_dispatch_history": False, "read_consistency": "live",
        "watermark": 698, "latest_seq": 698,
        "filters": {"status": None, "assigned_to": None, "include_closed": include_closed,
                    "include_archived": True, "review_unclaimed_only": False, "ticket_ids": None},
        **changes,
    }


class Transport:
    """Fake only the external RPC boundary; RawReader.call stays real."""
    def __init__(self, rows: list, details: dict, active: Any = None, all_page: Any = None, truncated: bool = False):
        self.rows, self.details = rows, details
        self.active, self.all_page, self.truncated = active, all_page, truncated
        self.gets = 0

    async def call_tool(self, name: str, arguments: dict) -> dict:
        assert arguments["board_id"] == "board-fe05"
        if name == "board_snapshot":
            return {"board": {"board_id": "board-fe05"}, "tickets": [], "agents": [], "truncated": self.truncated}
        if name == "ticket_list":
            # This is a real boundary contract, not an assertion on a mocked component.
            assert arguments.get("view") == "work"
            selected = self.all_page if arguments["include_closed"] else self.active
            if isinstance(selected, Exception):
                raise selected
            if selected is not None:
                return copy.deepcopy(selected)
            start = int(arguments.get("cursor", "synthetic:0").split(":")[1])
            end = start + arguments["limit"]
            more = end < len(self.rows)
            return copy.deepcopy(page(self.rows[start:end], arguments["include_closed"],
                total_matching=len(self.rows), has_more=more,
                next_cursor=f"synthetic:{end}" if more else None))
        if name == "ticket_get":
            assert arguments.get("view") == "work"
            self.gets += 1
            result = self.details.get(arguments["ticket_id"], {})
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        if name == "board_state_get":
            raise ValueError("state key not found")
        raise AssertionError("forbidden RPC: " + name)


def read(transport: Transport) -> dict:
    reader = coordinator.RawReader("https://isolated.invalid", "test-owned-opaque")
    reader._client = transport
    reader._decode = lambda value: value
    return asyncio.run(coordinator.read_board(reader, "board-fe05", "home"))[0]


def demand(fleet: ModuleType, snapshot: dict) -> Any:
    pooled = fleet.registry_fleet_snapshots({
        "home": {"agents": [], "tickets": [], "truncated": False}, "board-fe05": snapshot,
    }, {"home"}, "home")
    return fleet.fleet_snapshot_from_products(pooled, [], {
        "home": {"provider": {"status": "healthy", "latency_ms": 5}},
    }, {"load_ratio": 0.1, "capacity_available": True, "executor_status": "healthy"}, NOW).demands["home"]


def detail(row: dict, **changes: Any) -> dict:
    return {"ok": True, "ticket": row, "view": "work", "include_dispatch_history": False, "latest_seq": 698, **changes}


def test_complete_count_stub_recovers_real_work_metadata_into_pooled_fleet(central, fleet):
    row = work(central, ticket())
    oversized = stub(row)
    assert "tier" not in oversized
    transport = Transport([oversized], {row["ticket_id"]: detail(row)}, truncated=True)
    snapshot = read(transport)
    result = demand(fleet, snapshot)
    assert snapshot["coordination_tickets_complete"] is True
    assert result.open_by_tier == {1: 0, 2: 1, 3: 0}
    assert result.source_board_ids == ("board-fe05", "home")
    assert result.oldest_ticket_age_s == 600


@pytest.mark.parametrize("field", ["tier", "tags", "created_at", "dispatch_state", "status", "parked", "priority"])
def test_unmarked_operationally_incomplete_row_is_hydrated(central, fleet, field):
    full = work(central, ticket())
    partial = {key: value for key, value in full.items() if key != field}
    snapshot = read(Transport([partial], {full["ticket_id"]: detail(full)}))
    assert snapshot["coordination_tickets"][0][field] == full[field]
    result = demand(fleet, snapshot)
    assert result.open_by_tier == {1: 0, 2: 1, 3: 0}
    assert result.oldest_ticket_age_s == 600


BAD_PAGES = [
    {}, {"ok": False}, {"count": None, "total_matching": None},
    {"count": True, "total_matching": True}, {"count": "1", "total_matching": "1"},
    {"count": -1, "total_matching": -1}, {"count": 0, "total_matching": 0},
    {"returned_count": 0}, {"total_matching": 2}, {"has_more": True},
    {"next_cursor": "opaque"}, {"view": "summary"}, {"watermark": None},
    {"latest_seq": 699}, {"include_dispatch_history": True}, {"filters": {}},
    {"tickets": [None]}, {"tickets": [{"ticket_id": ""}]},
    {"tickets": [{"ticket_id": True}]},
    {"tickets": [ticket("TK/invalid")]}, {"tickets": [ticket(" TK-spaced ")]},
    {"filters": {"status": None, "assigned_to": None, "include_closed": 0,
                 "include_archived": 1, "review_unclaimed_only": 0, "ticket_ids": None}},
]


@pytest.mark.parametrize("changes", BAD_PAGES)
def test_malformed_or_partial_list_never_authorizes_fleet(central, fleet, changes):
    row = work(central, ticket())
    active = {} if changes == {} else page([row], **changes)
    snapshot = read(Transport([row], {}, active=active))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_successful_empty_work_pages_are_complete(fleet):
    snapshot = read(Transport([], {}))
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot["intake_tickets_complete"] is True
    assert demand(fleet, snapshot).open_by_tier == {1: 0, 2: 0, 3: 0}


@pytest.mark.parametrize("changes", [
    {"tags": None}, {"tags": [True]}, {"created_at": "invalid"},
    {"parked": None}, {"dispatch_state": "offered"},
    {"dispatch_state": {"state": "offered", "expires_at": "invalid"}},
    {"human_request": {"message": "unknown resolution"}},
    {"omitted_sections": ["tier"]}, {"oversized_for_list": 0},
    {"omitted_sections": ""}, {"dispatch_state": {"state": "offered"}},
    {"current_offer": {"state": "offered"}},
])
def test_still_incomplete_hydrated_operational_metadata_fails_closed(central, fleet, changes):
    full = work(central, ticket())
    snapshot = read(Transport([stub(full)], {full["ticket_id"]: detail({**full, **changes})}))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_list_exception_is_not_empty_complete(fleet):
    snapshot = read(Transport([], {}, active=TimeoutError("synthetic timeout")))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_duplicate_ids_do_not_hide_missing_demand(central, fleet):
    row = work(central, ticket())
    snapshot = read(Transport([row, row], {}))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


@pytest.mark.parametrize("kind", ["exception", "empty", "failed", "mismatch", "still-stub", "missing-tags", "marker", "watermark", "view"])
def test_failed_hydration_is_incomplete_not_fleet_demand(central, fleet, kind):
    row = work(central, ticket())
    responses = {
        "exception": TimeoutError("synthetic timeout"), "empty": {},
        "failed": detail(row, ok=False), "mismatch": detail({**row, "ticket_id": "TK-other"}),
        "still-stub": detail(stub(row)), "missing-tags": detail({k: v for k, v in row.items() if k != "tags"}),
        "marker": detail({**row, "omitted_sections": ["human_request"]}),
        "watermark": detail(row, latest_seq=699), "view": detail(row, view="summary"),
    }
    snapshot = read(Transport([stub(row)], {row["ticket_id"]: responses[kind]}))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_hydration_budget_exhaustion_is_bounded_and_incomplete(central, fleet):
    rows = [work(central, ticket("TK-" + str(i))) for i in range(40)]
    transport = Transport([stub(row) for row in rows], {row["ticket_id"]: detail(row) for row in rows})
    snapshot = read(transport)
    assert 0 < transport.gets <= 32
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


@pytest.mark.parametrize("tier", [None, True, False, 0, 4])
def test_genuinely_invalid_tier_is_not_defaulted_after_hydration(central, fleet, tier):
    row = work(central, ticket(tier=tier))
    snapshot = read(Transport([stub(row)], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets"][0]["tier"] is tier
    with pytest.raises(ValueError, match="ticket tier is invalid"):
        demand(fleet, snapshot)


def test_hydration_preserves_human_acp_age_offer_and_parked_filters(central, fleet):
    rows = [
        work(central, ticket("TK-parked", parked=True)),
        work(central, ticket("TK-blocked", human_request={"resolution": None})),
        work(central, ticket("TK-needs-human", status="needs_human")),
        work(central, ticket("TK-resolved", human_request={"resolution": {"answer": "ok"}})),
        work(central, ticket("TK-acp", tags=["acp-worker"], tier=3)),
        work(central, ticket("TK-offer", tier=1, dispatch_state={"state": "offered", **offer(ticket_id="TK-offer")})),
        work(central, ticket("TK-expired", tier=1, dispatch_state={"state": "offered", **offer(ticket_id="TK-expired", offered_at=(NOW - timedelta(seconds=61)).isoformat(), expires_at=(NOW - timedelta(seconds=1)).isoformat(), expires_at_epoch=(NOW - timedelta(seconds=1)).timestamp())})),
        work(central, ticket("TK-review", status="submitted")),
    ]
    result = demand(fleet, read(Transport([stub(row) for row in rows], {row["ticket_id"]: detail(row) for row in rows})))
    assert result.open_by_tier == {1: 2, 2: 1, 3: 0}
    assert (result.excluded_parked, result.excluded_blocked) == (1, 2)
    assert (result.acp_backlog, result.review_backlog, result.expiring_offers) == (1, 1, 1)
    assert result.oldest_ticket_age_s == 600


# Mutation caught: admitting false/scalar resolution skips unresolved-human exclusion.
@pytest.mark.parametrize("resolution", [False, True, 0, 1, "resolved", [], [None]])
def test_scalar_human_resolution_is_hydrated_or_fenced(central, fleet, resolution):
    malformed = work(central, ticket(human_request={"resolution": resolution}))
    snapshot = read(Transport([malformed], {malformed["ticket_id"]: detail(malformed)}))
    assert snapshot["coordination_tickets_complete"] is False
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


@pytest.mark.parametrize("key", ["dispatch_state", "current_offer"])
@pytest.mark.parametrize("value", [
    {"state": "unknown-invalid-state"}, {"state": "expired"},
    {"state": "offered", "expires_at": NOW.isoformat()},
    {"state": "broadcast"}, {"state": "claimed", "agent_id": False},
    {"state": "closed"}, {"state": "unassignable", "kind": "invalid", "reason": "no candidates"},
    {"state": "reopened", "at": False},
])
def test_illegal_or_partial_dispatch_metadata_is_hydrated_or_fenced(central, fleet, key, value):
    malformed = work(central, ticket())
    malformed[key] = value
    snapshot = read(Transport([malformed], {malformed["ticket_id"]: detail(malformed)}))
    assert snapshot["coordination_tickets_complete"] is False
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


@pytest.mark.parametrize("lease", [
    "unknown-invalid-lease", False, {}, {"reviewer_agent_id": "AI-holder"},
    review_lease(reviewer_agent_id=False), review_lease(expires_at="invalid"),
    review_lease(expires_at_epoch=False), review_lease(ttl_s=True),
    review_lease(reviewer_agent_name=""), review_lease(reviewer_principal_id=[]),
])
def test_malformed_present_review_lease_cannot_disappear_from_holder_protection(central, fleet, lease):
    malformed = work(central, ticket(status="submitted", review_lease=lease))
    snapshot = read(Transport([malformed], {malformed["ticket_id"]: detail(malformed)}))
    assert snapshot["coordination_tickets_complete"] is False
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


@pytest.mark.parametrize("field", ["claimed_by_agent_id", "claimed_by", "claimed_by_principal_id", "lease_expires_at", "ttl_s"])
def test_incomplete_work_holder_projection_is_detail_recovered(central, field):
    full = work(central, ticket(status="claimed", **work_claim()))
    malformed = {k: v for k, v in full.items() if k != field}
    snapshot = read(Transport([malformed], {full["ticket_id"]: detail(full)}))
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot["coordination_tickets"][0][field] == full[field]


@pytest.mark.parametrize("changes", [
    {"claimed_by_agent_id": False}, {"claimed_by": 1},
    {"claimed_by_principal_id": []}, {"lease_expires_at": "invalid"}, {"ttl_s": False},
])
def test_malformed_work_holder_is_fenced(central, changes):
    row = work(central, ticket(status="claimed", **work_claim(**changes)))
    snapshot = read(Transport([row], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is False


@pytest.mark.parametrize("changes", [
    {"human_request": {"resolution": None}},
    {"dispatch_state": {"state": "unknown-invalid-state"}},
    {"review_lease": "unknown-invalid-lease", "status": "submitted"},
])
def test_operational_recovery_preserves_real_holder_and_human_filters(central, fleet, changes):
    malformed = work(central, ticket(**changes))
    repaired_changes = {"human_request": {"resolution": None}}
    if "review_lease" in changes:
        repaired_changes = {"status": "submitted", "review_lease": review_lease()}
    full = work(central, ticket(**repaired_changes))
    # Mark the first case incomplete so even a legitimate unresolved HR is recovered.
    malformed.pop("tags")
    snapshot = read(Transport([malformed], {full["ticket_id"]: detail(full)}))
    assert snapshot["coordination_tickets_complete"] is True
    if "review_lease" in changes:
        observer = pure_module(ROOT / "tools/board-butler/fleet_observation.py", {"ticket_holds_seat"})
        row = snapshot["coordination_tickets"][0]
        assert observer.ticket_holds_seat(row, {"agent_id": "AI-holder"}) is True
        assert observer.ticket_holds_seat(row, {"agent_id": "AI-other"}) is False
    else:
        assert demand(fleet, snapshot).excluded_blocked == 1


LEGAL_DISPATCH = [
    {"state": "offered", **offer()},
    {"state": "broadcast", "kind": "work", "reason": "offer_limit_reached", "at": NOW.isoformat(), "cycle": 1},
    {"state": "unassignable", "kind": "work", "reason": "no_eligible_worker"},
    {"state": "unassignable", "kind": "work", "reason": "pinned_seat_unavailable", "pin_unavailable_cycles": 2},
    {"state": "claimed", "kind": "work", "agent_id": "AI-holder", "at": NOW.isoformat()},
    {"state": "review_claimed", "kind": "review", "agent_id": "AI-holder", "at": NOW.isoformat()},
    {"state": "parked", "kind": None, "at": NOW.isoformat()},
    {"state": "needs_human", "request_id": "HR-fixture", "at": NOW.isoformat()},
    {"state": "reopened", "reason": "human_input_resolved", "at": NOW.isoformat()},
    {"state": "closed", "kind": "review", "at": NOW.isoformat()},
    {"state": "rejected", "kind": "review", "at": NOW.isoformat()},
    {"state": "canceled", "at": NOW.isoformat()},
]


@pytest.mark.parametrize("dispatch", LEGAL_DISPATCH)
def test_deployed_legal_dispatch_shapes_are_not_falsely_rejected(central, dispatch):
    row = work(central, ticket(dispatch_state=dispatch))
    snapshot = read(Transport([row], {}))
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot["coordination_tickets"][0]["dispatch_state"] == dispatch


@pytest.mark.parametrize("epoch_offset", [0, 0.0000002])
@pytest.mark.parametrize(("key", "kind"), [("current_offer", "work"), ("work_offer", "work"), ("review_offer", "review")])
def test_legal_current_offer_is_preserved(central, epoch_offset, key, kind):
    row = work(central, ticket())
    row[key] = offer(kind=kind, expires_at_epoch=(NOW + timedelta(seconds=60)).timestamp() + epoch_offset)
    snapshot = read(Transport([row], {}))
    assert snapshot["coordination_tickets_complete"] is True


@pytest.mark.parametrize("status", ["claimed", "in_progress", "creating_report", "reviewing"])
def test_legal_work_holder_shape_is_complete_and_exact_holder_protected(central, status):
    row = work(central, ticket(status=status, **work_claim()))
    snapshot = read(Transport([row], {}))
    assert snapshot["coordination_tickets_complete"] is True
    observer = pure_module(ROOT / "tools/board-butler/fleet_observation.py", {"ticket_holds_seat"})
    protected = observer.ticket_holds_seat(snapshot["coordination_tickets"][0], {"agent_id": "AI-holder"})
    # The integrated consumer must protect every legitimate active work state.
    assert protected is True


@pytest.mark.parametrize("key", ["work_offer", "review_offer"])
def test_malformed_kind_specific_offer_is_fenced(central, key):
    row = work(central, ticket(**{key: "invalid"}))
    snapshot = read(Transport([row], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is False


@pytest.mark.parametrize("lease", [None, review_lease(expires_at_epoch=(NOW + timedelta(seconds=900)).timestamp() + 0.0000002)])
def test_valid_optional_review_lease_is_preserved(central, lease):
    row = work(central, ticket(status="submitted", review_lease=lease))
    snapshot = read(Transport([row], {}))
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot["coordination_tickets"][0]["review_lease"] == lease


def test_unknown_active_holder_stays_incomplete(central):
    row = work(central, ticket(status="claimed"))
    snapshot = read(Transport([row], {row["ticket_id"]: detail(row)}))
    assert snapshot["coordination_tickets_complete"] is False


def test_hydration_byte_budget_fails_closed(central, fleet):
    row = work(central, ticket(description="x" * 800_000))
    snapshot = read(Transport([stub(row)], {row["ticket_id"]: detail(row)}))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_hydration_time_budget_fails_closed(central, fleet, monkeypatch):
    row = work(central, ticket())

    class SlowTransport(Transport):
        async def call_tool(self, name, arguments):
            if name == "ticket_get":
                await asyncio.sleep(0.05)
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(coordinator, "TICKET_HYDRATION_TIMEOUT_SECONDS", 0.001, raising=False)
    snapshot = read(SlowTransport([stub(row)], {row["ticket_id"]: detail(row)}))
    assert snapshot.get("coordination_tickets_complete") is not True
    with pytest.raises(ValueError, match="incomplete"):
        demand(fleet, snapshot)


def test_fleet_source_board_spoof_remains_unauthorized(central, fleet):
    row = work(central, ticket())
    snapshot = read(Transport([row], {}))
    pooled = fleet.registry_fleet_snapshots({"home": {"agents": [], "tickets": []}, "board-fe05": snapshot}, {"home"}, "home")
    pooled["home"]["coordination_tickets"][0]["_fleet_source_board"] = "unauthorized"
    with pytest.raises(ValueError, match="source board is unauthorized"):
        fleet.fleet_snapshot_from_products(pooled, [], {}, {}, NOW)


def test_failed_all_ticket_list_is_not_complete_intake(central):
    row = work(central, ticket())
    snapshot = read(Transport([row], {}, all_page={}))
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot.get("intake_tickets_complete") is not True


@pytest.mark.parametrize("name", ["board_join", "ticket_create", "ticket_claim", "board_state_update"])
def test_raw_reader_rejects_join_and_writes(name):
    reader = coordinator.RawReader("https://isolated.invalid", "test-owned-opaque")
    reader._client = Transport([], {})
    reader._decode = lambda value: value
    with pytest.raises(RuntimeError, match="non-pure tool"):
        asyncio.run(reader.call(name, "board-fe05"))


@pytest.mark.parametrize("fault", [None, "watermark", "duplicate", "cursor_loop"])
def test_bounded_cursor_pages_preserve_complete_coverage(central, fault):
    first = work(central, ticket(ticket_id="TK-first"))
    second = work(central, ticket(ticket_id="TK-second"))
    class PagedTransport(Transport):
        async def call_tool(self, name, arguments):
            if name != "ticket_list": return await super().call_tool(name, arguments)
            cursor = arguments.get("cursor")
            changes = {"total_matching": 2}
            if cursor:
                row = first if fault == "duplicate" else second
                if fault == "watermark": changes.update(watermark=699, latest_seq=699)
                if fault == "cursor_loop": changes.update(has_more=True, next_cursor="page-two")
                return page([row], arguments["include_closed"], **changes)
            return page([first], arguments["include_closed"], has_more=True,
                        next_cursor="page-two", **changes)
    snapshot = read(PagedTransport([], {}))
    assert snapshot["coordination_tickets_complete"] is (fault is None)
    if fault is None:
        assert [row["ticket_id"] for row in snapshot["coordination_tickets"]] == ["TK-first", "TK-second"]
        assert snapshot["intake_tickets_complete"] is True


def test_large_rows_reduce_page_size_before_spending_detail_budget(central):
    rows = [work(central, ticket(ticket_id=f"TK-{index}")) for index in range(2)]
    class ByteLimitedTransport(Transport):
        async def call_tool(self, name, arguments):
            if name != "ticket_list": return await super().call_tool(name, arguments)
            if arguments["limit"] > 1:
                return page([stub(row) for row in rows], arguments["include_closed"])
            index = 1 if arguments.get("cursor") else 0
            return page([rows[index]], arguments["include_closed"], total_matching=2,
                        has_more=index == 0, next_cursor="last" if index == 0 else None)
    transport = ByteLimitedTransport([], {})
    snapshot = read(transport)
    assert snapshot["coordination_tickets_complete"] is True
    assert snapshot["intake_tickets_complete"] is True
    assert transport.gets == 0
