"""Disposable Central workflow-to-delivery exercise; no real credentials or PRs."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT / "tools" / "board-butler"))

import board_butler as butler
from pursers_client.project_workflow import (
    final_validation_ticket_id, parse_project_workflow, policy_digest,
)

for name in ("integration_delivery", "workflow_delivery"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "board-butler" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
import integration_delivery as delivery_runtime
import workflow_delivery as workflow_runtime

HOME_BOARD = "pursers"
BOARD = "sonar-work"
NOW = butler.datetime(2026, 10, 25, tzinfo=butler.timezone.utc)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


class _PrConnector:
    """Approved connector boundary fake; Git and delivery runtime remain real."""
    def __init__(self):
        self.prs = []
        self.create_calls = 0

    async def list_customer_prs(self, repository, target_branch, status):
        return [p for p in self.prs if p["target_branch"] == target_branch and p["status"] == status]

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.create_calls += 1
        pr = {"id": self.create_calls, "status": "active", "mutable": False,
              "source_sha": payload["snapshot_sha"], "source_branch": payload["source_branch"],
              "target_branch": payload["target_branch"], "correlation": payload["correlation"]}
        self.prs.append(pr)
        return {"status": "confirmed", "pr": pr}

    async def reconcile_operation(self, repository, reservation):
        return {"status": "unknown"}

    async def get_customer_pr(self, repository, pr_id):
        return next(row for row in self.prs if row["id"] == pr_id)


def _git_runtime(tmp_path: Path, ticket_ids):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.name", "Disposable QA")
    _git(seed, "config", "user.email", "qa@example.invalid")
    (seed / "README").write_text("baseline\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "baseline")
    base = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "HEAD:refs/heads/main")
    branches, shas = {}, {}
    for ticket, filename in zip(ticket_ids, ("foundation.txt", "feature.txt")):
        branch = f"workflow/{ticket}"
        _git(seed, "switch", "-c", branch, "main")
        (seed / filename).write_text(f"accepted {ticket}\n")
        _git(seed, "add", filename)
        _git(seed, "commit", "-m", f"approved {ticket}")
        shas[ticket] = _git(seed, "rev-parse", "HEAD")
        branches[ticket] = branch
        _git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
    _git(seed, "switch", "main")
    _git(seed, "push", "origin", "main:refs/heads/customer/review")
    clone = tmp_path / "delivery-clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    connector = _PrConnector()
    adapter = delivery_runtime.VerifiedGitConnectorAdapter(
        clone, str(bare), connector, validation_commands=["git status --porcelain"])
    runtime = delivery_runtime.BatchDeliveryRuntime(
        delivery_runtime.BatchLedger(tmp_path / "batch-ledger.json"), adapter)
    policy = {"mode": "batch_pr", "repository": str(bare), "base_branch": "main",
        "integration_branch": "pursers/workflow-integration", "snapshot_prefix": "pursers/snapshots",
        "target_branch": "customer/review", "trigger": "ready", "snapshot_strategy": "frozen",
        "policy_revision": "f" * 40}
    return runtime, policy, connector, clone, base, branches, shas


def _workflow(ticket_ids):
    raw = {"schema_version": 1, "workflow_id": "workflow:native-e2e",
        "project_id": "NativeE2E", "board_id": BOARD, "cohort_id": "cohort:native-e2e",
        "sealed": True, "nodes": [
            {"node_id": "foundation", "ticket_id": ticket_ids[0], "requires": [],
             "acceptance_checks": ["independent exact-SHA review"]},
            {"node_id": "feature", "ticket_id": ticket_ids[1], "requires": ["foundation"],
             "acceptance_checks": ["independent exact-SHA review"]}],
        "final_validation": {"title": "Validate the complete native cohort",
            "acceptance_checks": ["Run actual checks on the exact combined integration head"]}}
    normalized = parse_project_workflow(raw, project_id="NativeE2E", board_id=BOARD)
    raw_activation = {"schema_version": 1, "state": "active",
        "policy_sha256": policy_digest(normalized), "activation_id": "operator:fixture-approval"}
    return raw, raw_activation


def test_disposable_central_source_intake_to_one_qa_gated_pr(tmp_path, monkeypatch):
    """Real Central mutations/projections and Git orchestration, fake only PR transport.

    The delivery factory is the agreed narrow test seam.  This deliberately
    asserts that the native reconciliation path actually calls it: merely
    patching an unused binding must not make the end-to-end gate pass.
    """
    import pursers_central.central as central
    from test_managed_intake import PagedClient, _declaration, _issue, _source
    from test_source_intake import butler as intake_butler
    coordinator_module = __import__("runpy").run_path(str(ROOT / "tools" / "coordinator" / "coordinator.py"))

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys":[]}')
    for key, value in {"CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp", "CENTRAL_JWKS_PATH": str(jwks),
        "CENTRAL_ADMISSION": "invite", "STORE_BACKEND": "sqlite"}.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server("localhost", 8765, tmp_path / "central")
    principal = lambda name, caps: central.Principal("PR-" + name, name, frozenset(caps))
    admin = principal("admin", {"board:read", "board:write", "board:review", "board:coordinate", "board:intake"})
    coordinator_agent = principal("coordinator", {"board:read", "board:write", "board:coordinate"})
    intake_agent = principal("intake", {"board:read", "board:coordinate", "board:intake"})
    worker = principal("worker", {"board:read", "board:write"})
    reviewer = principal("reviewer", {"board:read", "board:write", "board:review"})
    current = {"principal": admin}
    monkeypatch.setattr(central, "current_principal", lambda: current["principal"])
    # A synthetic credential exists only in this disposable Central instance;
    # mimic the authenticated proof boundary for raw in-process tool calls.
    fixture_token = "disposable-central-test-token-never-reused"
    monkeypatch.setattr(central, "get_access_token", lambda: SimpleNamespace(token=fixture_token))
    now = NOW

    async def call(who, tool, *, board_id=BOARD, **args):
        current["principal"] = who
        if tool == "ticket_submit" and "branch_and_commit" in args.get("notes", ""):
            match = __import__("re").search(r"(?m)^branch_and_commit: (.+?) @ ([0-9a-f]{40})$", args["notes"])
            assert match
            branch, sha = match.groups()
            preflight = {"kind": "git-ls-remote-exact-tip-v1", "branch": branch,
                "commit": sha, "remote_ref": f"origin/{branch}", "remote_tip": sha}
            proof_payload = json.dumps({"board_id": BOARD, "ticket_id": args["ticket_id"],
                "agent_name": args["agent_name"], **preflight}, sort_keys=True, separators=(",", ":")).encode()
            args["submission_preflight"] = {**preflight,
                "proof": hmac.new(fixture_token.encode(), proof_payload, hashlib.sha256).hexdigest()}
        result = await mcp.call_tool(tool, {"board_id": board_id, **args})
        assert not result.is_error, (tool, result)
        return result.structured_content

    async def scenario():
        await call(admin, "board_join", agent_name="admin")
        await call(admin, "board_join", board_id=HOME_BOARD, agent_name="admin-home")
        for who, role in ((coordinator_agent, "member"), (intake_agent, "member"), (worker, "member"), (reviewer, "reviewer")):
            service.mutate(BOARD, lambda doc, who=who, role=role: doc["principal_memberships"].update({
                who.principal_id: {"principal_id": who.principal_id, "role": role,
                    "source": "disposable-test", "created_at": now.isoformat()}}))
        await call(coordinator_agent, "board_join", agent_name="fixture-coordinator", role="coordinator",
                   capabilities={"can_work": False, "can_review": False})
        await call(intake_agent, "board_join", agent_name="fixture-intake", role="coordinator",
                   capabilities={"can_work": False, "can_review": False})
        worker_join = await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        await call(reviewer, "board_join", agent_name="fixture-reviewer", role="reviewer",
            capabilities={"can_work": False, "can_review": True, "tier_max": 3, "max_parallel": 1})
        # Register a valid nonhome project route before managed source intake.
        # The source explicitly maps its Sonar project hint to this registered project.
        repo_url = "https://example.invalid/NativeE2E.git"
        project_row = {"board_id": BOARD, "status": "active", "fleet": True,
            "work_dir": str(tmp_path), "fleet_clone_dir": str(tmp_path),
            "work_dir_owner": "fleet", "repository_url": repo_url}
        registry = {"schema_version": 1, "projects": {"NativeE2E": project_row}}
        await call(admin, "board_state_update", board_id=HOME_BOARD, agent_name="admin-home",
            key="project_registry", value=json.dumps(registry))

        source_calls = []
        # Reuse the managed-intake test's policy-backed fake connector; only its
        # upstream Sonar response is synthetic, while all Central writes are real.
        source_decl = _declaration()
        source_client = PagedClient({1: [_issue(1), _issue(2)]}, source_calls)
        source_runtime = __import__("test_managed_intake")._runtime(source_decl, source_client)
        async def read_source_state(board_id):
            assert board_id == BOARD
            try: return await call(admin, "board_state_get", key="coordinator_intake")
            except Exception as error:
                if "state key not found" in str(error): return None
                raise
        async def write_source_state(board_id, value, expected):
            assert board_id == BOARD
            return await call(admin, "board_state_update", agent_name="admin",
                key="coordinator_intake", value=value, expected_sha256=expected)
        async def read_source_ticket(board_id, ticket_id):
            assert board_id == BOARD
            try: return (await call(admin, "ticket_get", ticket_id=ticket_id, view="full"))["ticket"]
            except Exception as error:
                if "not found" in str(error): return None
                raise
        async def annotate_source_ticket(board_id, ticket_id, text):
            assert board_id == BOARD
            return await call(admin, "ticket_annotate", agent_name="admin",
                ticket_id=ticket_id, kind="evidence", text=text)
        source_config = _source(writeback={"on": "approved", "tool": "pr_create",
            "arg_template": {"repositoryId": "example-repo", "sourceRefName": "refs/heads/{source_branch}",
                "targetRefName": "refs/heads/{target_branch}", "title": "{ticket_id}"}})
        source_config["routing"] = {"project_map": {"alpha": "NativeE2E"}}
        source = intake_butler.parse_source_declarations(
            [source_config], {"connector:sonar": source_decl})[0]
        source_index = intake_butler.SourceIntakeIndex(tmp_path / "source-index.json")
        poller = intake_butler.SourceIntakePoller(
            sources=[source], runtimes={"connector:sonar": source_runtime},
            registry_projects={"NativeE2E": BOARD}, state_reader=read_source_state,
            state_writer=write_source_state, ticket_reader=read_source_ticket,
            ticket_annotator=annotate_source_ticket, index=source_index,
            per_source_cap=2, cycle_cap=2)
        async def source_ceiling(_in_flight): return 2
        async def source_decision(_context): return {"pull": 2, "source_ids": ["sonar"], "reason": "fixture"}
        poller.ceiling, poller.decide = source_ceiling, source_decision
        intake_result = await poller.run_cycle(NOW)
        assert intake_result["new_asks"] == 2, intake_result
        intake_state = await read_source_state(BOARD)
        assert intake_state is not None
        creates = []
        async def create_from_approved_intake(_board, draft):
            assert _board == BOARD
            result = await call(intake_agent, "ticket_create", agent_name="fixture-intake",
                ticket_id=draft.ticket_id, title=draft.title, description=draft.description,
                scope=draft.scope, target_url=draft.target_url,
                required_fields=list(draft.required_fields), category=draft.category,
                unassigned=True, coordinator_op_key=draft.op_key)
            creates.append(result["ticket"]["ticket_id"])
            return creates[-1]
        # Source-declared auto approval is exercised by the existing coordinator
        # intake processor, with the registered work project mapping and real tickets.
        findings, _updates = await coordinator_module["process_intakes"](
            [coordinator_module["Project"]("NativeE2E", BOARD, tmp_path, domain="work")],
            {BOARD: {"coordinator_intake_state": intake_state,
                     "intake_tickets_complete": True, "intake_tickets": []}},
            NOW, coordinator_module["RuntimeState"].for_mode("active"), enabled=True,
            dry_run=False, create_ticket=create_from_approved_intake, rate_per_hour=None)
        ticket_ids = list(creates)
        assert len(ticket_ids) == 2 and len(set(ticket_ids)) == 2, findings
        source_by_external = {entry["external_id"]: entry for entry in source_index.entries.values()}
        assert set(source_by_external) == {"SONAR-1", "SONAR-2"}, source_by_external
        assert {entry["ticket_id"] for entry in source_by_external.values()} == set(ticket_ids)
        source_ticket_ids = {}
        tickets = {}
        for ticket_id in ticket_ids:
            row = (await call(admin, "ticket_get", ticket_id=ticket_id, view="full"))["ticket"]
            tickets[ticket_id] = row
            assert row["target_url"] == "NativeE2E/"
            source_payload = json.loads(row["description"].split("--- BEGIN SOURCE DATA ---\n", 1)[1].split("\n--- END SOURCE DATA ---", 1)[0])
            source_ticket_ids[source_payload["external_id"]] = ticket_id
            assert source_payload["external_id"] in {"SONAR-1", "SONAR-2"}
        assert source_ticket_ids == {external: entry["ticket_id"] for external, entry in source_by_external.items()}
        workflow, activation = _workflow(ticket_ids)
        runtime, delivery_policy, connector, clone, base, branches, shas = _git_runtime(tmp_path, ticket_ids)
        repo_url = delivery_policy["repository"]
        project = SimpleNamespace(name="NativeE2E", board_id=BOARD,
            status="active", fleet=True,
            butler_workflow=workflow, butler_workflow_activation=activation,
            fleet_clone_dir=str(clone), work_dir=str(clone), work_dir_owner="fleet",
            repository_url=repo_url)
        registry["projects"]["NativeE2E"].update({
            "work_dir": str(clone), "fleet_clone_dir": str(clone),
            "butler_workflow": workflow, "butler_workflow_activation": activation})
        await call(admin, "board_state_update", board_id=HOME_BOARD, agent_name="admin-home",
            key="project_registry", value=json.dumps(registry))
        # Load the exact registered policy from Central rather than trusting
        # the test's pre-write object. Coordinator read_cycle and Butler's
        # refresh handoff are asserted below against this resident state.
        registry_wire = await call(admin, "board_state_get", board_id=HOME_BOARD, key="project_registry")
        registry_projects = coordinator_module["parse_registry"](registry_wire)
        assert [(row.name, row.board_id) for row in registry_projects] == [("NativeE2E", BOARD)]
        resident_registry = json.loads(registry_wire["state"]["value"])
        assert resident_registry["projects"]["NativeE2E"]["butler_workflow_activation"] == activation
        registry = resident_registry
        await call(admin, "ticket_update", agent_name="admin", ticket_id=ticket_ids[1], parked=True)
        tickets[ticket_ids[1]] = (await call(admin, "ticket_get", ticket_id=ticket_ids[1], view="full"))["ticket"]
        args = SimpleNamespace(
            runtime_mode="active", url="http://disposable.invalid/mcp", home_board=HOME_BOARD,
            repo=ROOT, token_path=tmp_path / "token", agent_name="fixture-butler",
            dry_run=True, kill_switch=False, veto_question=None, act_on_board=[BOARD],
            active_action=[], no_live_candidates_cycles=3, action_hold_seconds=0,
        )
        backend = butler.CentralBackend(args, "disposable")
        # Bind the exact poller and persisted index that admitted the two real
        # Central intake tickets; source member lookup may not use a fake index.
        backend.source_intake_poller = poller
        assert backend.source_intake_poller.index is source_index
        assert source_index.path.is_file()
        called = []

        async def delivery_factory(_project):
            called.append(True)
            return runtime, delivery_policy
        backend._workflow_delivery_runtime = delivery_factory

        @asynccontextmanager
        async def client_for_board(board):
            assert board == BOARD
            class Client:
                async def ticket_update(self, ticket_id, **kwargs):
                    return await call(coordinator_agent, "ticket_update", agent_name="fixture-coordinator", ticket_id=ticket_id, **kwargs)
                async def ticket_create(self, ticket_id, title, **kwargs):
                    assert isinstance(kwargs.get("description"), str)
                    return await call(coordinator_agent, "ticket_create", agent_name="fixture-coordinator",
                        ticket_id=ticket_id, title=title, **kwargs)
                async def ticket_annotate(self, ticket_id, text, **kwargs):
                    return await call(admin, "ticket_annotate", ticket_id=ticket_id,
                        text=text, agent_name="admin", **kwargs)
                async def ticket_history_list(self, ticket_id, history, **kwargs):
                    return await call(admin, "ticket_history_list", ticket_id=ticket_id,
                        history=history, agent_name="admin", **kwargs)
            yield Client()
        backend._client_for_board = client_for_board

        # Exercise CentralBackend.refresh_registry_findings and the actual
        # coordinator.read_cycle against disposable Central wire operations.
        # Only its network reader boundary is adapted; registry and board data
        # are fetched from the real disposable service.
        class DisposableRawReader:
            def __init__(self, _url, _token):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return None
            async def call(self, name, board_id, **kwargs):
                return await call(admin, name, board_id=board_id, **kwargs)

        coordinator_module["RawReader"] = DisposableRawReader
        backend._coordinator = coordinator_module
        async def no_coordinator_side_effects(_args):
            return None
        coordinator_module["run"] = no_coordinator_side_effects
        backend._reconcile_fleet = lambda *_args: asyncio.sleep(0, result={})
        backend._observation_context_for_board = lambda *_args, **_kwargs: asyncio.sleep(0, result={})
        backend._harvest_approval_scan = lambda *_args: None
        backend._schedule_approval_scan = lambda *_args: {"status": "fixture"}
        backend._schedule_source_intake_refresh = lambda *_args: {"status": "fixture-not-scheduled"}
        backend._approval_findings_for_board = lambda *_args: []
        monkeypatch.setattr(butler, "derive_board_observations", lambda _context: [])
        monkeypatch.setattr(butler, "observation_replay_metrics", lambda *_args: {
            "open_questions": 0, "reconciled_open_questions": 0,
            "repeat_rediscovery_escalations": 0})
        refresh = await backend.refresh_registry_findings(NOW)
        assert refresh["active_boards"] == [BOARD]
        assert backend._workflow_registry is not None, repr(backend._workflow_registry_error)
        assert backend._workflow_registry["projects"][project.name]["board_id"] == BOARD
        assert backend._workflow_registry["projects"][project.name]["butler_workflow_activation"] == activation
        assert poller.workflow_owned == backend._source_ticket_workflow_owned
        assert await poller.workflow_owned(BOARD, tickets[ticket_ids[0]]) is True
        assert backend._workflow_registry_error is None

        # Incomplete source/cohort projection and a foreign project route both
        # fail closed before any Git delivery preparation or Central effects.
        incomplete = await backend._reconcile_project_workflows(
            BOARD, [project], {ticket_ids[0]: tickets[ticket_ids[0]]}, registry=registry)
        assert any(row.get("reason") == "sealed_manifest_projection_incomplete" for row in incomplete)
        foreign = {**tickets, ticket_ids[1]: {**tickets[ticket_ids[1]], "target_url": "Elsewhere"}}
        blocked = await backend._reconcile_project_workflows(BOARD, [project], foreign, registry=registry)
        assert any(row.get("reason") == "sealed_manifest_project_route_mismatch" for row in blocked)
        assert not runtime.ledger.document["batches"] and connector.prs == []

        foundation, feature = ticket_ids
        await call(admin, "ticket_assign", agent_name="admin", ticket_id=foundation,
            assigned_to_agent_id=worker_join["agent_id"], expected_status="open",
            coordinator_op_key="assign-" + foundation, reason="Disposable workflow test")
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=foundation)
        sha = shas[foundation]
        notes = f"branch_and_commit: {branches[foundation]} @ {sha}\ncommit_hash: {sha}\ntest_output: source checks passed"
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=foundation,
            summary="Source validated", notes=notes, files_changed=["foundation.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=foundation,
            verdict="approve", review_notes="Independent exact-commit fixture review")
        tickets[foundation] = (await call(admin, "ticket_get", ticket_id=foundation, view="full"))["ticket"]

        # Use unmodified ticket_get output: these actual Central projections
        # retain decision histories but intentionally have no fabricated latest
        # aliases. Native reconciliation must hydrate/judge the immutable rows.
        assert tickets[foundation]["status"] == "closed"
        assert "latest_submission" not in tickets[foundation]
        assert "latest_verdict" not in tickets[foundation]
        assert tickets[foundation]["submission_history"][-1]["commit_hash"] == shas[foundation]
        assert tickets[foundation]["review_history"][-1]["submission_commit"] == shas[foundation]
        assert tickets[feature]["parked"] is True
        assert backend.args.runtime_mode == "active"
        assert project.board_id == BOARD and BOARD != HOME_BOARD
        from pursers_client.project_workflow import parse_workflow_activation
        assert parse_workflow_activation(project.butler_workflow_activation,
            parse_project_workflow(workflow, project_id=project.name, board_id=BOARD))
        first = await backend._reconcile_project_workflows(BOARD, [project], tickets, registry=registry)
        tickets[feature] = (await call(admin, "ticket_get", ticket_id=feature, view="full"))["ticket"]
        assert tickets[feature].get("parked") is False, first
        await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=feature)
        sha = shas[feature]
        notes = f"branch_and_commit: {branches[feature]} @ {sha}\ncommit_hash: {sha}\ntest_output: source checks passed"
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=feature,
            summary="Source validated", notes=notes, files_changed=["feature.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=feature,
            verdict="approve", review_notes="Independent exact-commit fixture review")
        tickets[feature] = (await call(admin, "ticket_get", ticket_id=feature, view="full"))["ticket"]
        assert tickets[feature]["status"] == "closed"
        assert "latest_submission" not in tickets[feature]
        assert "latest_verdict" not in tickets[feature]
        assert tickets[feature]["submission_history"][-1]["commit_hash"] == shas[feature]
        assert tickets[feature]["review_history"][-1]["submission_commit"] == shas[feature]
        # The legacy per-source writeback sees an actually approved source
        # ticket, but activated workflow ownership must suppress its PR path.
        legacy = await poller.run_cycle(NOW)
        assert legacy["writebacks"] == 0 and connector.prs == []
        assert not [name for name, _ in source_calls if name == "pr_create"]
        assert {entry["status"] for entry in source_index.entries.values()} == {"asked"}
        assert source_index.in_flight() == {"sonar": 2}
        first = await backend._reconcile_project_workflows(BOARD, [project], tickets, registry=registry)
        assert called, ("native reconciliation did not use the agreed delivery runtime factory", first)
        qa_id = final_validation_ticket_id(
            parse_project_workflow(workflow, project_id=project.name, board_id=BOARD)
        )
        assert any(a.get("ticket_id") == qa_id for a in first), first
        qa = (await call(admin, "ticket_get", ticket_id=qa_id, view="full"))["ticket"]
        assert qa["status"] == "open"
        from pursers_client.project_workflow import parse_final_validation_packet_index
        packet_index = parse_final_validation_packet_index(qa["description"])
        assert packet_index["project_id"] == project.name
        assert packet_index["board_id"] == BOARD
        pages = [row["text"] for row in qa.get("annotations", [])
                 if isinstance(row, dict) and row.get("text", "").startswith("butler-workflow-packet:v1:")]
        assert len(pages) == packet_index["evidence_packet"]["chunks"]
        parsed_pages = {}
        for page in pages:
            match = __import__("re").fullmatch(
                r"butler-workflow-packet:v1:([0-9a-f]{64}):(\d+)/(\d+):(.*)", page, __import__("re").S)
            assert match
            digest, page_number, page_count, chunk = match.groups()
            assert digest == packet_index["evidence_packet"]["sha256"]
            assert int(page_count) == len(pages)
            parsed_pages[int(page_number)] = chunk
        assert set(parsed_pages) == set(range(1, len(pages) + 1))
        encoded_packet = "".join(parsed_pages[index] for index in range(1, len(pages) + 1))
        assert hashlib.sha256(encoded_packet.encode()).hexdigest() == packet_index["evidence_packet"]["sha256"]
        packet_payload = json.loads(encoded_packet)
        assert packet_payload["workflow_id"] == workflow["workflow_id"]
        assert packet_payload["project_id"] == project.name and packet_payload["board_id"] == BOARD
        assert packet_payload["combined_revision"] == packet_index["combined_revision"]
        assert set(packet_payload["members"]) == set(ticket_ids)
        for ticket_id in ticket_ids:
            index_entry = next(entry for entry in source_index.entries.values()
                               if entry["ticket_id"] == ticket_id)
            assert packet_payload["members"][ticket_id] == shas[ticket_id]
            member_evidence = packet_payload["member_evidence"][ticket_id]
            assert member_evidence["issue_ids"] == [index_entry["external_id"]]
            assert member_evidence["evidence"]["source_issue_ids"] == [index_entry["external_id"]]
        assert packet_payload["workflow_delivery"]["combined_revision"] == packet_payload["combined_revision"]
        prepared_delivery = packet_payload["workflow_delivery"]
        delivery_attestation = workflow_runtime.workflow_delivery_attestation(prepared_delivery)
        assert delivery_attestation.startswith("workflow_delivery_sha256: ")
        source_members = [
            {"ticket_id": ticket_id, "origin": "managed-source",
             "issue_ids": packet_payload["member_evidence"][ticket_id]["issue_ids"]}
            for ticket_id in sorted(packet_payload["members"])
        ]
        # Unknown or incomplete outcomes cannot release durable intake capacity.
        for uncertain in ({"state": "unknown"}, {"state": "in_delivery"},
                          {"state": "integration_pending", "pr_id": "42"}):
            assert await backend._settle_workflow_source_delivery(
                project.name, BOARD, source_members, qa_id, prepared_delivery, uncertain,
                runtime, delivery_policy) == 0
        assert {row["status"] for row in source_index.entries.values()} == {"asked"}
        assert source_index.in_flight() == {"sonar": 2}
        assert all("native_workflow_pr_id" not in row for row in source_index.entries.values())
        assert len(runtime.ledger.document["batches"]) == 1
        batch = next(iter(runtime.ledger.document["batches"].values()))
        combined = batch["working_head"]
        assert combined not in shas.values()
        assert _git(clone, "show", f"{combined}:foundation.txt").startswith("accepted")
        assert _git(clone, "show", f"{combined}:feature.txt").startswith("accepted")
        assert connector.prs == []

        # Source submissions use stay_active=False, which may deactivate the
        # worker seat. Rejoin before assigning the final QA task.
        await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        await call(admin, "ticket_assign", agent_name="admin", ticket_id=qa_id,
            assigned_to_agent_id=worker_join["agent_id"], expected_status="open",
            coordinator_op_key="assign-" + qa_id, reason="QA fixture assignment")
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=qa_id)
        qa_commit = combined
        qa_notes = (f"branch_and_commit: pursers/native-qa @ {qa_commit}\n"
                    f"commit_hash: {qa_commit}\ntest_output: exact combined SHA {combined}; all checks passed\n"
                    f"{delivery_attestation}")
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=qa_id,
            summary="Exact combined cohort checks passed", notes=qa_notes,
            files_changed=["validation-report.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=qa_id,
            verdict="approve", review_notes=f"validated_sha: {combined}\n{delivery_attestation}\nIndependent review of the exact prepared combined SHA")
        qa = (await call(admin, "ticket_get", ticket_id=qa_id, view="full"))["ticket"]
        assert qa["status"] == "closed"
        assert "latest_submission" not in qa and "latest_verdict" not in qa
        assert qa["submission_history"][-1]["commit_hash"] == combined
        assert qa["review_history"][-1]["submission_commit"] == combined
        # Simulate a crash after confirmed PR creation but before the source
        # index's atomic save; a fresh index must recover from the exact ledger.
        original_save = source_index.save
        save_attempts = []
        def fail_terminal_save_once():
            if all(entry.get("status") == "delivered" for entry in source_index.entries.values()):
                save_attempts.append(True)
                if len(save_attempts) == 1:
                    raise OSError("simulated crash before source-index atomic replace")
            return original_save()
        source_index.save = fail_terminal_save_once
        failed_persist = await backend._reconcile_project_workflows(
            BOARD, [project], {**tickets, qa_id: qa}, registry=registry)
        assert any(row.get("reason") == "OSError" for row in failed_persist), failed_persist
        assert save_attempts == [True]
        assert len(connector.prs) == 1
        stale_index = intake_butler.SourceIntakeIndex(source_index.path)
        assert all(entry["status"] == "asked" for entry in stale_index.entries.values())
        assert stale_index.in_flight() == {"sonar": 2}
        poller.index = stale_index
        source_index = stale_index
        recovered = await backend._reconcile_project_workflows(
            BOARD, [project], {**tickets, qa_id: qa}, registry=registry)
        assert all(entry["status"] == "delivered" for entry in source_index.entries.values()), (failed_persist, recovered)
        assert source_index.in_flight() == {}
        assert len(connector.prs) == 1, (first, failed_persist, recovered, qa.get("status"),
            qa.get("submission_history"), qa.get("review_history"), packet_payload.get("workflow_delivery"))
        assert connector.prs[0]["source_sha"] == combined
        assert connector.prs[0]["target_branch"] == "customer/review"
        refs_before = {ref: _git(clone, "rev-parse", f"refs/remotes/origin/{ref}")
            for ref in ("main", "customer/review")}
        await backend._reconcile_project_workflows(BOARD, [project], {**tickets, qa_id: qa}, registry=registry)
        assert len(connector.prs) == 1
        assert refs_before == {ref: _git(clone, "rev-parse", f"refs/remotes/origin/{ref}")
            for ref in refs_before}, "workflow delivery changed the environment/base refs"
        assert base == refs_before["main"] == refs_before["customer/review"]
        # Confirmed native workflow delivery settles the durable source index.
        assert {entry["status"] for entry in source_index.entries.values()} == {"delivered"}
        assert source_index.in_flight() == {}
        assert all(entry.get("native_workflow_qa_ticket_id") == qa_id
                   and entry.get("native_workflow_pr_id") == str(connector.prs[0]["id"])
                   and entry.get("native_workflow_commit") == combined
                   and entry.get("delivery_state") == "pr_created"
                   for entry in source_index.entries.values())
        persisted_index = intake_butler.SourceIntakeIndex(source_index.path)
        assert persisted_index.entries == source_index.entries
        assert persisted_index.in_flight() == {}

    asyncio.run(scenario())
