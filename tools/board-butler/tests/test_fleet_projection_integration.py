"""Incomplete product reads cannot authorize local fleet effects."""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fleet_reconciler import NOW, butler, fleet_executor
from test_fleet_observation import fixture, observer_api


def complete_board():
    return {"agents": [], "tickets": [], "truncated": False,
            "omitted_counts": {"agents": 0, "tickets": 0}}


@pytest.mark.parametrize("fault", ["explicit_incomplete", "missing_board", "bad_omissions",
    "negative_omissions", "boolean_omissions", "nonmapping_ticket", "duplicate_ticket",
    "nonmapping_agent", "missing_tier", "boolean_tier", "unhashable_status",
    "unbacked_complete", "invalid_complete_flag"])
def test_incomplete_registry_blocks_before_any_fleet_effect(tmp_path, fault):
    board = complete_board()
    if fault == "explicit_incomplete": board["coordination_tickets_complete"] = False
    elif fault == "bad_omissions": board["omitted_counts"] = []
    elif fault == "negative_omissions": board["omitted_counts"]["agents"] = -1
    elif fault == "boolean_omissions": board["omitted_counts"]["agents"] = False
    elif fault == "nonmapping_ticket": board["tickets"] = [None]
    elif fault == "nonmapping_agent": board["agents"] = [None]
    elif fault == "unbacked_complete": board["coordination_tickets_complete"] = True
    elif fault == "invalid_complete_flag": board["coordination_tickets_complete"] = "true"
    elif fault == "unhashable_status": board["tickets"] = [{"ticket_id": "TK-one", "status": []}]
    elif fault == "duplicate_ticket":
        row = {"ticket_id": "TK-one", "status": "open", "tier": 1}
        board["tickets"] = [row, dict(row)]
    elif fault in {"missing_tier", "boolean_tier"}:
        board["tickets"] = [{"ticket_id": "TK-one", "status": "open"}]
        if fault == "boolean_tier": board["tickets"][0]["tier"] = True
    snapshots = {"home": complete_board(), "other": board}
    if fault == "missing_board": snapshots.pop("other")
    options = SimpleNamespace(runtime_mode="active", home_board="home",
        fleet_observation_file=tmp_path/"observation", fleet_state_file=tmp_path/"state",
        fleet_executor_socket=tmp_path/"socket", fleet_executor_key_id="fixture",
        fleet_executor_private_key=tmp_path/"private", fleet_observation_mode="local")
    backend = butler.CentralBackend(options, "test-owned")
    backend.client = object()
    marker = tmp_path/"fleet-effect"
    async def effect(*args, **kwargs):
        marker.write_text("unexpected mutation")
        raise AssertionError("fleet side effect crossed an incomplete projection")
    backend._apply_host_seat_cap_commands = effect
    backend._collect_local_fleet_observation = effect
    backend._autonomous_fleet_configs = effect
    result = asyncio.run(backend._reconcile_fleet(["home", "other"], snapshots, NOW))
    assert result["status"] == "blocked"
    assert result["reason_code"] == "fleet_snapshot_incomplete"
    assert "other" in result["blocked_boards"]
    assert not marker.exists()
    assert not options.fleet_state_file.exists()


def test_disabled_fleet_does_not_inspect_or_mutate_incomplete_input():
    backend = butler.CentralBackend(SimpleNamespace(), "test-owned")
    assert asyncio.run(backend._reconcile_fleet(["home"], {}, NOW)) == {"status": "disabled"}


def test_inventory_preserves_explicit_incomplete_projection():
    snapshot = complete_board()
    snapshot["tickets"] = [{"ticket_id": "TK-partial", "status": "open"}]
    class Client:
        async def board_snapshot(self, **kwargs): return copy.deepcopy(snapshot)
        async def _call(self, *args): return {"members": []}
    backend = butler.CentralBackend(SimpleNamespace(), "test-owned")
    @asynccontextmanager
    async def client_for_board(board): yield Client()
    backend._client_for_board = client_for_board
    previous = {"home": {**snapshot, "coordination_tickets_complete": False,
                          "truncated": True, "omitted_counts": {"agents": 0, "tickets": 1}}}
    inventory, _ = asyncio.run(backend._fleet_registry_inventory(["home"], previous))
    assert inventory["home"]["coordination_tickets_complete"] is False
    assert inventory["home"]["truncated"] is True


@pytest.mark.parametrize("value", [None, [], "0", {"agents": -1}, {"agents": False}])
def test_malformed_agent_omission_metadata_is_not_a_complete_view(value):
    with pytest.raises(butler.IdentityConflict):
        butler.assert_complete_agent_view({"agents": [], "omitted_counts": value})


@pytest.mark.parametrize("value", [None, [], "0", {"agents": -1}, {"agents": False}])
def test_observer_rejects_malformed_omissions_without_recovery(value):
    template, boards, members, service = fixture()
    boards["b"]["omitted_counts"] = value
    recovered = []
    observer = observer_api()["LocalFleetObserver"]({"t": template}, service, {},
        {"t": {"board_id": "a", "provider": "worker"}},
        recover=lambda *a, **k: recovered.append(a))
    observation, readiness, leases = observer.collect(["a", "b"], boards, members,
        NOW, {"worker": {"status": "healthy", "latency_ms": 1}}, {})
    assert observation["executor_seats"][0]["lifecycle"] == "unhealthy"
    assert all("seat" not in leases["boards"][b]["seats"] for b in ("a", "b"))
    assert not recovered


@pytest.mark.parametrize("holder,expected", [("agent", True), ("other", False), (None, True)])
def test_creating_report_preserves_exact_or_unknown_holder(holder, expected):
    api = observer_api()
    ticket = {"ticket_id": "TK-report", "status": "creating_report"}
    if holder is not None: ticket["claimed_by_agent_id"] = holder
    assert api["ticket_holds_seat"](ticket, {"agent_id": "agent"}) is expected


def test_incomplete_control_plane_does_not_schedule_source_intake(tmp_path):
    async def exercise():
        backend = butler.CentralBackend(SimpleNamespace(), "test-owned")
        backend._source_projection_complete = False
        marker = tmp_path/"source-work"
        class Poller:
            async def run_cycle(self, now): marker.write_text("unexpected source work")
        backend.source_intake_poller = Poller()
        result = backend._schedule_source_intake_refresh(NOW)
        await asyncio.sleep(0)
        assert result["status"] == "blocked"
        assert result["reason_code"] == "fleet_snapshot_incomplete"
        assert not marker.exists()
    asyncio.run(exercise())


def test_incomplete_control_plane_blocks_intake_ceiling_and_model(tmp_path):
    backend = butler.CentralBackend(SimpleNamespace(), "test-owned")
    backend._source_projection_complete = False
    marker = tmp_path/"provider-or-config-call"
    async def external(*args, **kwargs):
        marker.write_text("unexpected request")
        raise AssertionError("incomplete observation reached external request")
    backend.coordinator_config = external
    backend._client_for_board = external
    async def exercise():
        assert await backend._source_intake_ceiling({}) == 0
        decision = await backend._source_intake_decide({"ceiling": 10})
        assert decision["pull"] == 0 and decision["model_called"] is False
        assert not marker.exists()
    asyncio.run(exercise())


def test_intake_queue_load_uses_recovered_complete_projection():
    snapshot = {"agents": [], "tickets": [{"status": "open"}],
        "coordination_tickets_complete": True,
        "coordination_tickets": [{"status": "open"}, {"status": "open"}, {"status": "submitted"}]}
    assert butler.source_intake_board_load(snapshot, NOW) == {
        "open": 2, "submitted": 1, "idle_workers": 0, "idle_reviewers": 0}
