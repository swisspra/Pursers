"""Both intake routes survive real Central byte-cap projection and hydration."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client
from pursers_client.client import BoardClient
from test_managed_intake import PagedClient, _declaration, _runtime, _source, _issue
from test_source_intake import butler

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("dual_intake_coordinator", ROOT/"tools/coordinator/coordinator.py")
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)
BOARD = "pursers"


@pytest.mark.parametrize("route", ["sonar", "coordinator"])
def test_actual_central_intake_to_hydrated_fleet_demand(tmp_path, monkeypatch, route):
    import pursers_central.central as central
    jwks = tmp_path/"jwks.json"; jwks.write_text('{"keys":[]}')
    for key,value in {"CENTRAL_AUTH_MODE":"jwt", "CENTRAL_JWT_ISSUER":"https://issuer.example.invalid",
        "CENTRAL_JWT_AUDIENCE":"http://localhost:8765/mcp", "CENTRAL_JWKS_PATH":str(jwks),
        "CENTRAL_ADMISSION":"invite", "STORE_BACKEND":"sqlite"}.items(): monkeypatch.setenv(key,value)
    mcp,service = central.build_server("localhost",8765,tmp_path/"central")
    main = central.Principal("PR-coordinator", "coordinator",
        frozenset({"board:read", "board:write", "board:coordinate"}))
    intake = central.Principal("PR-coordinator", "coordinator", frozenset({"board:read", "board:intake"}))
    current = {"principal":main}
    monkeypatch.setattr(central,"current_principal",lambda:current["principal"])
    (tmp_path/"repository").mkdir()
    now = datetime.now(timezone.utc)
    registry = {"schema_version":1,"projects":{"Alpha":{
        "board_id":BOARD,"work_dir":str(tmp_path/"repository"),"status":"active","domain":"work"}}}
    protocol = {}
    async def call(name, **arguments):
        return BoardClient._decode(await protocol["client"].call_tool(name,{"board_id":BOARD,**arguments}))
    async def write_state(board,value,expected):
        assert board == BOARD
        await call("board_state_update", agent_name="fixture-coordinator",key="coordinator_intake",
                   value=value,expected_sha256=expected)
    async def read_state(board):
        assert board == BOARD
        try:return await call("board_state_get",key="coordinator_intake")
        except Exception as error:
            if "state key not found" in str(error):return None
            raise
    async def read_ticket(board,ticket_id):
        assert board == BOARD
        try:return (await call("ticket_get",ticket_id=ticket_id,view="full"))["ticket"]
        except Exception as error:
            if "not found" in str(error) or "unknown ticket" in str(error):return None
            raise
    async def annotate(board,ticket_id,text):
        await call("ticket_annotate",agent_name="fixture-coordinator",ticket_id=ticket_id,kind="evidence",text=text)
    class Transport:
        async def call_tool(self,name,arguments):return await protocol["client"].call_tool(name,arguments)
    async def scenario():
        # Admit only this disposable test instance's coordinator fixture.
        service.mutate(BOARD, lambda doc: doc["principal_memberships"].update({
            "PR-coordinator": {"principal_id": "PR-coordinator", "role": "admin",
                               "source": "test", "created_at": now.isoformat()}}))
        await call("board_join",agent_name="fixture-coordinator",role="orchestrator",
                   capabilities={"can_work":False,"can_review":False})
        await call("board_state_update",agent_name="fixture-coordinator",key="project_registry",value=json.dumps(registry))
        await call("board_dispatch_policy_set", agent_name="fixture-coordinator", default_ticket_tier=1)
        reader = coordinator.RawReader("http://synthetic.invalid", "test-owned")
        reader._client=Transport();reader._decode=BoardClient._decode
        if route == "sonar":
            issue=_issue(1);issue["details"]="Fix the same parser bug with a regression check. "*20
            runtime=_runtime(_declaration(),PagedClient({1:[issue]},[]))
            async def ceiling(in_flight):return 1
            async def decide(context):return {"pull":1,"source_ids":["sonar"],"reason":"fixture capacity"}
            poller=butler.SourceIntakePoller(
                sources=butler.parse_source_declarations([_source()],{"connector:sonar":runtime.declaration}),
                runtimes={"connector:sonar":runtime},registry_projects={"Alpha":BOARD},
                state_reader=read_state,state_writer=write_state,ticket_reader=read_ticket,ticket_annotator=annotate,
                index=butler.SourceIntakeIndex(tmp_path/"source-index.json"),ceiling=ceiling,decide=decide)
            result=await poller.run_cycle(now)
            assert result["new_asks"] == 1, json.dumps(result)
        else:
            ask={"id":"human-request","text":"Fix the parser bug and preserve the existing response contract. "*20,
                 "requested_by":"fixture-human","board_id":BOARD,"approved":True,
                 "approved_by":"fixture-human","approved_at":now.isoformat()}
            await write_state(BOARD,json.dumps({"schema_version":1,"asks":[ask],"tombstones":[]}),None)
        snapshot,_=await coordinator.read_board(reader,BOARD,"home")
        async def create(board,draft):
            assert board == BOARD
            current["principal"]=intake
            try:
                created=await call("ticket_create",agent_name="fixture-coordinator",ticket_id=draft.ticket_id,
                    title=draft.title,description=draft.description,scope=draft.scope,
                    required_fields=list(draft.required_fields),target_url=draft.target_url,
                    tags=["coordinator-intake",f"op:{draft.op_key}"],unassigned=True,coordinator_op_key=draft.op_key)
            finally:current["principal"]=main
            assert created["ticket"]["tier"] == 1, created["ticket"]["tier"]
            return created["ticket"]["ticket_id"]
        findings,updates=await coordinator.process_intakes(
            [coordinator.Project("Alpha",BOARD,tmp_path/"repository",domain="work")],
            {BOARD:snapshot},now,coordinator.RuntimeState.for_mode("active"),enabled=True,dry_run=False,
            create_ticket=create,rate_per_hour=None)
        assert any(row["kind"] == "intake-created" for row in findings),json.dumps(findings)
        assert len(updates[BOARD]) == 1
        monkeypatch.setattr(central,"DEFAULT_PAGE_MAX_BYTES",8192)
        raw=await call("ticket_list",view="work",include_closed=False,limit=500)
        assert raw["tickets"][0]["oversized_for_list"] is True
        assert "tier" not in raw["tickets"][0]
        recovered,_=await coordinator.read_board(reader,BOARD,"home")
        assert recovered["coordination_tickets_complete"] is True
        assert recovered["intake_tickets_complete"] is True
        assert recovered["coordination_tickets"][0]["tier"] == 1
        assert butler.fleet_projection_error(recovered) is None
        fleet=butler.fleet_snapshot_from_products({BOARD:recovered},[],
            {BOARD:{"worker":{"status":"healthy","latency_ms":1}}},
            {"load_ratio":0.1,"capacity_available":True,"executor_status":"healthy"},now)
        assert fleet.demands[BOARD].open_by_tier == {1:1,2:0,3:0}
    async def exercise():
        async with Client(mcp, mode="2026-07-28", cache=None) as client:
            protocol["client"] = client
            await scenario()
    asyncio.run(exercise())
