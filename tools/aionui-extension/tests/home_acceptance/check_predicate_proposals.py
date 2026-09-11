#!/usr/bin/env python3
"""Fail-closed validator for the TK-fadd47fce924 predicate delta."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DELTA = ROOT / "docs/design-home/context/typed-predicate-integration-delta.json"
FACTS_PATH = "docs/design-home/context/acceptance-facts.json"
BASE = "d09757f2e6187fd417ee1017814e92754047ea54"
FLEET_DASHBOARD = ROOT / "tools/fleet-dashboard/fleet_dashboard.py"
BROWSER_OBSERVER = ROOT / "tools/aionui-extension/tests/home_acceptance/browser_observer.py"
WEBUI_APP = ROOT / "tools/aionui-extension/webui/app.js"
EXTENSION_MANIFEST = ROOT / "tools/aionui-extension/aion-extension.json"
CONTEXT_BINDINGS = [
    "observation_id", "run_id", "action_id", "entity", "surface", "board_id",
    "candidate_commit", "issued_at", "causal_index",
]
PARENT_PRODUCER_IDS = {
    "fleet.add-project-registry",
    "fleet.add-project-principals",
    "fleet.add-project-policy",
    "fleet.add-project-clone-steps",
    "fleet.operations-rollback-failure",
    "fleet.release-central",
    "fleet.release-ci",
    "fleet.release-github",
    "fleet.release-pypi",
    "fleet.release-restart-checklist",
}
EXPECTED_CONTRACT_GAPS = {
    "fleet.add-project-idempotent-rerun",
    "fleet.doors-disabled",
}
PRESET_FACTS = {
    "extension.worker-preset-codex": "pursers-worker-codex",
    "extension.worker-preset-claude": "pursers-worker-claude",
    "extension.reviewer-preset-codex": "pursers-reviewer-codex",
    "extension.reviewer-preset-claude": "pursers-reviewer-claude",
}
CREDENTIAL_GAP_FIELDS = [
    "/_evidence/changed", "/_evidence/effect", "/doors",
    "/steps/5/step", "/steps/5/status",
]
DOOR_DISABLED_GAP_FIELDS = [
    "/before/disabled", "/action/outcome", "/action/during/disabled",
    "/after/disabled",
]
OPS = {"eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"}
POINTER = re.compile(r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$")


class Invalid(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    require(actual == expected, f"{label}: keys {sorted(actual)} != {sorted(expected)}")


def git_json(commit: str, path: str) -> dict[str, Any]:
    raw = subprocess.check_output(
        ["git", "show", f"{commit}:{path}"], cwd=ROOT, text=True
    )
    return json.loads(raw)


def anchor_text(commit: str, path: str) -> list[str]:
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", commit)),
        f"non-immutable source ref: {commit}",
    )
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{commit}:{path}"], cwd=ROOT, text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        raise Invalid(f"missing source anchor {commit}:{path}: {error.output.strip()}") from error
    return raw.splitlines()


def all_facts(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["sequence"] + document["inventory"] + document["final_gates"]


def check_partition(delta: dict[str, Any]) -> None:
    parent = all_facts(git_json(BASE, FACTS_PATH))
    measured = [
        fact["id"]
        for fact in parent
        if fact["derivation"].get("kind") == "collector_gap"
    ]
    partition = delta["partition"]
    owned = partition["owned_ids"]
    complement = partition["complement_ids"]
    proposals = delta["proposals"]
    require(delta["base_commit"] == BASE, "wrong working base")
    require(len(parent) == 201, "parent must contain 201 acceptance facts")
    require(
        measured == owned,
        "owned_ids must exactly preserve the parent's collector-gap order",
    )
    require(len(owned) == len(set(owned)) == 121, "owned partition must be 121 unique IDs")
    require(len(complement) == len(set(complement)) == 43, "complement must be 43 unique IDs")
    require(set(owned).isdisjoint(complement), "partition overlap")
    require(
        [item["id"] for item in proposals] == owned,
        "proposal order/coverage differs from owned_ids",
    )
    require(partition["owned_count"] == 121, "owned_count mismatch")
    require(partition["complement_count"] == 43, "complement_count mismatch")
    require(partition["collector_gap_count"] == 121, "collector_gap_count mismatch")


def check_source_anchors(proposals: list[dict[str, Any]]) -> int:
    anchors: dict[tuple[str, str, tuple[int, int]], None] = {}
    for item in proposals:
        source = item["source"]
        anchors[(source["commit"], source["path"], tuple(source["lines"]))] = None
        for supporting in source.get("supporting_sources", []):
            anchors[(
                supporting["source_sha"], supporting["source_path"],
                tuple(supporting["source_lines"]),
            )] = None
    for commit, path, lines in anchors:
        require(len(lines) == 2, f"{path}: source lines must be a closed pair")
        start, end = lines
        text = anchor_text(commit, path)
        require(1 <= start <= end <= len(text), f"{commit}:{path}:{start}-{end} out of range")
    require(len(anchors) == 127, f"expected 127 unique source anchors, got {len(anchors)}")
    return len(anchors)


def check_pointer(path: Any, label: str, *, empty_ok: bool = False) -> None:
    if empty_ok and path == "":
        return
    require(isinstance(path, str) and POINTER.fullmatch(path) is not None, f"{label}: bad pointer {path!r}")
    generic = re.fullmatch(r"/(?:target|operation)(?:_\d+)?", path)
    require("*" not in path and generic is None, f"{label}: open/generic pointer {path}")


def normalized_predicate(predicate: dict[str, Any]) -> str:
    return json.dumps(
        {"kind": predicate["kind"], "assertions": predicate["assertions"]},
        sort_keys=True, separators=(",", ":"),
    )


def assertion_key(kind: str, assertion: dict[str, Any]) -> tuple[str, str]:
    if kind == "state_transition":
        return assertion["phase"], assertion["path"]
    if kind == "http_response":
        return assertion["target"], assertion["path"]
    return "field", assertion["path"]


def comparison(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "contains":
        if isinstance(actual, dict) and isinstance(expected, dict):
            return all(actual.get(key) == value for key, value in expected.items())
        if isinstance(actual, list) and isinstance(expected, list):
            return all(value in actual for value in expected)
        return expected in actual
    if op == "in":
        return actual in expected
    if op == "gt":
        return actual > expected
    if op == "gte":
        return actual >= expected
    if op == "lt":
        return actual < expected
    if op == "lte":
        return actual <= expected
    raise Invalid(f"unsupported operator {op}")


def evaluate(predicate: dict[str, Any], observed: dict[tuple[str, str], Any]) -> bool:
    return all(
        assertion_key(predicate["kind"], assertion) in observed
        and comparison(
            observed[assertion_key(predicate["kind"], assertion)],
            assertion["op"], assertion["value"],
        )
        for assertion in predicate["assertions"]
    )


def synthesized_observed(predicate: dict[str, Any]) -> dict[tuple[str, str], Any]:
    observed: dict[tuple[str, str], Any] = {}
    for assertion in predicate["assertions"]:
        key = assertion_key(predicate["kind"], assertion)
        expected = copy.deepcopy(assertion["value"])
        op = assertion["op"]
        if op == "ne":
            value = "__different__" if expected != "__different__" else "__other__"
        elif op == "in":
            require(isinstance(expected, list) and expected, "empty in operand")
            value = copy.deepcopy(expected[0])
        elif op == "gt":
            value = expected + 1
        elif op == "lt":
            value = expected - 1
        else:
            value = expected
        if key not in observed or assertion["op"] != "contains":
            observed[key] = value
        elif isinstance(value, str):
            observed[key] = f"{observed[key]} {value}"
        elif isinstance(value, list):
            observed[key] = list(dict.fromkeys([*observed[key], *value]))
        elif isinstance(value, dict):
            observed[key].update(value)
        else:
            raise Invalid("unsynthesizable contains value")
    return observed


def incompatible_value(assertion: dict[str, Any]) -> Any:
    expected = assertion["value"]
    op = assertion["op"]
    if op == "ne":
        return copy.deepcopy(expected)
    if op == "contains":
        if isinstance(expected, str):
            return "__wrong_value__"
        if isinstance(expected, list):
            return []
        if isinstance(expected, dict):
            return {}
    if op == "in":
        return "__outside_allowlist__"
    if op in {"gt", "gte"}:
        return expected - 1
    if op in {"lt", "lte"}:
        return expected + 1
    if isinstance(expected, bool):
        return not expected
    if isinstance(expected, (int, float)):
        return expected + 1
    if expected is None:
        return "__not_null__"
    return "__wrong_value__"


def pointer_value(document: Any, pointer: str) -> Any:
    value = document
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class ProducerCache:
    """Small deterministic dependency behind the real Fleet HTTP handler."""

    def labels(self) -> list[str]:
        return ["default"]

    def add_project(
        self, _name: str, _board_id: str, _work_dir: str,
        _integration_ref: str, _seats: Any,
    ) -> dict[str, Any]:
        return {
            "steps": [
                {"step": "registry_admin", "status": "created"},
                {"step": "board_create", "status": "created"},
                {"step": "door_principals", "status": "created"},
                {"step": "policies", "status": "configured"},
                {"step": "fleet_clone", "status": "prepared"},
                {"step": "door_credentials", "status": "issued"},
            ]
        }


class ProducerSeats:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def release_status(self) -> dict[str, Any]:
        def package(
            availability: str, present: bool | None, **rest: Any,
        ) -> dict[str, Any]:
            return {"availability": availability, "present": present, **rest}
        return {
            "central_version": {
                "live_version": "0.1.0a30", "staged_version": "0.1.0a30",
                "status": "matches",
            },
            "ci_status": {
                "main": {
                    "status": "completed", "conclusion": "success",
                    "url": "https://ci.example/main",
                },
                "tag": {
                    "status": "completed", "conclusion": "failure",
                    "url": "https://ci.example/tag",
                },
            },
            "github_release": {
                "present": True,
                "url": "https://github.example/releases/v5.0.0b1",
            },
            "pypi": {
                "pursers": package("present", True, status_code=200),
                "central": package("absent", False, status_code=404),
                "client": package(
                    "unavailable", None,
                    error="TimeoutError: bounded request timed out",
                ),
            },
            "restart_checklist": [
                {
                    "host": "claude-desktop", "needs_restart": False,
                    "reason": "Running current attributed bridge shim",
                    "running_pids": [202],
                },
                {
                    "host": "codex", "needs_restart": True,
                    "reason": "Bridge PID 102 started before shim update",
                    "running_pids": [102],
                },
            ],
        }

    def ops_action(self, plan_id: str, digest: str) -> dict[str, Any]:
        require(bool(plan_id and digest), "producer fixture action identifiers")
        job_id = uuid.uuid4().hex
        rollback = "ROLLBACK FAILED: profile.env: PermissionError; staged wheel: OSError"
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "failed",
            "error": f"Stage failed; {rollback}",
            "logs": [rollback],
        }
        return {"job_id": job_id, "status": "queued"}

    def job(self, job_id: str) -> dict[str, Any]:
        return self.jobs[job_id]


def producer_call(
    port: int, method: str, path: str, body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw = None if body is None else json.dumps(body, separators=(",", ":"))
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=raw, headers=headers)
    response = connection.getresponse()
    status = response.status
    document = json.loads(response.read())
    connection.close()
    return status, document


def check_parent_producer_examples(proposals: list[dict[str, Any]]) -> int:
    """Run proposal selections through inherited Fleet and page/helper producers."""
    require(
        subprocess.run(
            ["git", "diff", "--quiet", BASE, "--", str(FLEET_DASHBOARD.relative_to(ROOT))],
            cwd=ROOT,
        ).returncode == 0,
        "Fleet producer differs from the approved full parent",
    )
    spec = importlib.util.spec_from_file_location(
        "predicate_proposal_fleet_dashboard", FLEET_DASHBOARD
    )
    require(spec is not None and spec.loader is not None, "cannot load Fleet producer")
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dashboard
    spec.loader.exec_module(dashboard)
    seats = ProducerSeats()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            ProducerCache(), worker_manager=SimpleNamespace(), seat_manager=seats,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project_status, project = producer_call(
            server.server_port, "POST", "/api/projects/add",
            {
                "name": "sandbox-project", "board_id": "sandbox-board",
                "work_dir": "/PATH/TO/WORK", "integration_ref": "main",
            },
        )
        release_status, release = producer_call(
            server.server_port, "GET", "/api/config/release"
        )
        action_status, action = producer_call(
            server.server_port, "POST", "/api/config/ops",
            {"plan_id": "verifier-plan", "digest": "verifier-digest"},
        )
        job_id = action.get("job_id")
        require(
            action_status == 200
            and isinstance(job_id, str)
            and re.fullmatch(r"[a-f0-9]{32}", job_id) is not None,
            "Fleet action did not return a real job ID",
        )
        job_status, job = producer_call(
            server.server_port, "GET", f"/api/config/jobs/{job_id}"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    checked = 0
    for item in proposals:
        if item["id"] not in PARENT_PRODUCER_IDS:
            continue
        request = item["executable_request"]["recorder"]["request"]
        if request["path"] == "/api/projects/add":
            status, document = project_status, project
        elif request["path"] == "/api/config/release":
            status, document = release_status, release
        else:
            require(
                request["path"] == "/api/config/jobs/" + "0" * 32,
                f"{item['id']}: job path must use the declared substitution sentinel",
            )
            status, document = job_status, job
        observed = {
            ("action_origin", ""): "verifier_api",
            ("status", ""): status,
        }
        for pointer in request["select"]:
            observed[("field", pointer)] = pointer_value(document, pointer)
        require(
            evaluate(item["canonical_predicate"], observed),
            f"{item['id']}: actual parent producer output did not pass",
        )
        checked += 1
    require(checked == len(PARENT_PRODUCER_IDS), "parent Fleet producer example coverage")
    registration = next(
        item for item in proposals
        if item["id"] == "extension.environment-free-mcp-registration"
    )
    check_page_helper_producer_example(registration)
    return checked + 1


def check_page_helper_producer_example(item: dict[str, Any]) -> None:
    """Exercise the real recovery route and its closed page-owned capture contract."""
    transport = {
        "type": "stdio", "command": "pursers-wait-bridge", "args": [], "env": {},
    }
    expected_action = {
        "kind": "click_response_json",
        "selector": "#recover-seat",
        "method": "POST",
        "endpoint": "/pursers/onboarding/recover",
        "pointer": "/body/mcp_definition/transport",
        "path": "/mcp_transport",
    }
    recorder = item["executable_request"]["recorder"]
    require(recorder["action"] == [expected_action], "registration must use the exact recovery click")
    require(
        recorder["before"] == [{
            "path": "/recover_control", "selector": "#recover-seat", "property": "text",
        }],
        "registration before context drift",
    )
    require(
        recorder["after"] == [{
            "path": "/registration_status", "selector": "#connection-message",
            "property": "text",
        }],
        "registration after context drift",
    )

    app = WEBUI_APP.read_text()
    for source_fragment in (
        "api('/pursers/onboarding/recover', { json: payload })",
        "$('#recover-seat').addEventListener('click', recoverConnection)",
        "Registration recovered without replaying the door.",
    ):
        require(source_fragment in app, f"actual page recovery source missing {source_fragment!r}")

    script = r"""
const { createHandlers } = require('./tools/aionui-extension/webui/routes.js');
(async () => {
  const runBridge = async (args) => {
    if (args[0] !== 'status') throw new Error('unexpected bridge mutation');
    return 'push_mode=push\nboard=sandbox-board role=worker kid=door-1 exp=2000000000 seat_names_used=worker-three\n';
  };
  const handlers = createHandlers({
    expectedBoard: 'sandbox-board',
    allowedOrigin: 'http://127.0.0.1:25808',
    runBridge,
    importMcp: async () => ({ success: true, imported: false }),
  });
  const response = await handlers.handle(new Request(
    'http://127.0.0.1:43121/pursers/onboarding/recover', {
      method: 'POST',
      headers: { origin: 'http://127.0.0.1:25808' },
      body: JSON.stringify({
        board: 'sandbox-board', role: 'worker', seat_name: 'worker-three',
        tier_max: 2, folder: 'worker-three',
      }),
    },
  ));
  process.stdout.write(JSON.stringify({ status: response.status, body: await response.json() }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True,
    )
    require(completed.returncode == 0, f"actual helper recovery failed: {completed.stderr.strip()}")
    envelope = json.loads(completed.stdout)
    selected = pointer_value(envelope, expected_action["pointer"])
    require(selected == transport, "actual helper recovery transport drift")
    observed = {
        ("before", "/recover_control"): "Recover registration",
        ("action", "/mcp_transport"): selected,
        ("after", "/registration_status"):
            "Registration recovered without replaying the door.",
    }
    require(evaluate(item["canonical_predicate"], observed), "actual page/helper output did not pass")

    require(
        [{**expected_action, "selector": "#decoy"}] != recorder["action"],
        "wrong click target was accepted",
    )
    observer_spec = importlib.util.spec_from_file_location(
        "predicate_proposal_browser_observer", BROWSER_OBSERVER
    )
    require(observer_spec is not None and observer_spec.loader is not None, "cannot load browser observer")
    observer = importlib.util.module_from_spec(observer_spec)
    observer_spec.loader.exec_module(observer)
    try:
        observer._validate_transition_actions([
            expected_action, {**expected_action, "path": "/duplicate_transport"},
        ])
    except observer.ObserverError:
        pass
    else:
        raise Invalid("duplicate response capture was accepted")
    for cleanup_marker in (
        "window.clearTimeout(state.timer)",
        "window.fetch = state.original",
        "delete window[key]",
        "response capture did not match exactly once",
    ):
        require(cleanup_marker in observer.EGO_TRANSITION_SCRIPT, f"capture cleanup missing {cleanup_marker!r}")


def check_fact_specific_mutations(proposals: list[dict[str, Any]]) -> int:
    cases = (
        ("extension.join-progress", ("before", "/connect_step"), "Wrong prior step"),
        (
            "extension.worker-preset-codex",
            ("action", "/assistant_binding"),
            {"agent_id": "claude"},
        ),
        (
            "fleet.refresh-pause-resume",
            ("before", "/paused_status"),
            "Updated without pause",
        ),
        (
            "personal.work-priority",
            ("before", "/personal_work_priority_target_hidden"),
            False,
        ),
    )
    for fact_id, key, wrong in cases:
        item = next(proposal for proposal in proposals if proposal["id"] == fact_id)
        predicate = item["canonical_predicate"]
        observed = synthesized_observed(predicate)
        require(evaluate(predicate, observed), f"{fact_id}: representative positive failed")
        observed[key] = wrong
        require(not evaluate(predicate, observed), f"{fact_id}: fact-specific mutation passed")
    return len(cases)


def check_assistant_binding_examples(proposals: list[dict[str, Any]]) -> int:
    """Bind each proposal to the approved installed/runtime assistant adapter."""
    manifest_bytes = EXTENSION_MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    assistants = {
        item["id"]: item for item in manifest["contributes"]["assistants"]
    }
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    checked = 0
    for fact_id, assistant_id in PRESET_FACTS.items():
        assistant = assistants[assistant_id]
        context_file = assistant["contextFile"]
        context_sha256 = hashlib.sha256(
            (EXTENSION_MANIFEST.parent / context_file).read_bytes()
        ).hexdigest()
        action = {
            "kind": "assistant_binding",
            "endpoint": "/api/extensions/assistants",
            "assistant_id": assistant_id,
            "path": "/assistant_binding",
        }
        binding = {
            "manifest_id": assistant_id,
            "runtime_id": f"ext-{assistant_id}",
            "agent_id": assistant["agentId"],
            "preset_agent_type": assistant["presetAgentType"],
            "context_file": context_file,
            "context_sha256": context_sha256,
            "manifest_sha256": manifest_sha256,
            "extension_name": manifest["name"],
            "endpoint": "/api/extensions/assistants",
            "transport": "same-origin-http",
        }
        item = next(proposal for proposal in proposals if proposal["id"] == fact_id)
        recorder = item["executable_request"]["recorder"]
        require(recorder["action"] == [action], f"{fact_id}: assistant action drift")
        require(
            item["adapter"]["adapter"] == "aionui_assistant_binding_v1"
            and item["adapter"]["parent_contract_ref"] == BASE,
            f"{fact_id}: assistant adapter drift",
        )
        observed = {
            ("before", "/selected_board"): "sandbox-board",
            ("action", "/assistant_binding"): binding,
            ("after", "/selected_board"): "sandbox-board",
        }
        require(
            evaluate(item["canonical_predicate"], observed),
            f"{fact_id}: exact assistant binding did not pass",
        )
        mutated = copy.deepcopy(observed)
        mutated[("action", "/assistant_binding")]["preset_agent_type"] = "wrong"
        require(
            not evaluate(item["canonical_predicate"], mutated),
            f"{fact_id}: preset substitution passed",
        )
        checked += 1
    require(checked == 4, "assistant binding proposal coverage")
    nodes = [
        "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py::"
        "test_aionui_assistant_binding_joins_installed_manifest_and_runtime",
        "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py::"
        "test_aionui_assistant_binding_rejects_runtime_substitution",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *nodes],
        cwd=ROOT, text=True, capture_output=True,
    )
    require(
        completed.returncode == 0,
        "approved assistant binding regression failed: "
        + (completed.stdout + completed.stderr).strip()[-500:],
    )
    return checked


def check_contract_gap(item: dict[str, Any], keys: set[str]) -> None:
    exact_keys(item, keys, item["id"])
    require(item["status"] == "contract_gap", f"{item['id']}: gap status")
    require(item["executable_request"] is None, f"{item['id']}: gap request must be null")
    require(item["canonical_predicate"] is None, f"{item['id']}: gap predicate must be null")
    exact_keys(
        item["adapter"],
        {
            "support_status", "channel", "missing_channel_or_operation",
            "bounded_parent_correction",
        },
        f"{item['id']}.adapter",
    )
    require(item["adapter"]["support_status"] == "contract_gap", f"{item['id']}: gap adapter")
    require(item["validation"]["executable_now"] is False, f"{item['id']}: gap executable marker")
    requested = item["requested_semantics"]
    exact_keys(
        requested,
        {"kind", "source_id", "operation", "selection", "response_fields"},
        f"{item['id']}.requested_semantics",
    )
    if item["id"] == "fleet.add-project-idempotent-rerun":
        require(
            requested["kind"] == "http_response"
            and requested["source_id"] == "fleet-api",
            f"{item['id']}: gap kind/source",
        )
        expected_fields = CREDENTIAL_GAP_FIELDS
    else:
        require(
            requested["kind"] == "state_transition"
            and requested["source_id"] == f"browser-state-{item['id']}",
            f"{item['id']}: gap kind/source",
        )
        expected_fields = DOOR_DISABLED_GAP_FIELDS
    require(
        requested["response_fields"] == expected_fields,
        f"{item['id']}: gap response fields",
    )


def check_predicate(item: dict[str, Any]) -> int:
    predicate = item["canonical_predicate"]
    exact_keys(predicate, {"kind", "source_id", "assertions"}, f"{item['id']}.predicate")
    kind = predicate["kind"]
    require(kind in {"state_transition", "http_response", "mcp_tool_response"}, f"{item['id']}: bad kind")
    assertions = predicate["assertions"]
    require(isinstance(assertions, list) and assertions, f"{item['id']}: empty assertions")
    if kind == "state_transition":
        require(
            {assertion.get("phase") for assertion in assertions}
            == {"before", "action", "after"},
            f"{item['id']}: state transition must enforce before/action/after",
        )
    seen_assertions: set[str] = set()
    for assertion in assertions:
        if kind == "state_transition":
            exact_keys(assertion, {"phase", "path", "op", "value"}, f"{item['id']}.assertion")
            require(assertion["phase"] in {"before", "action", "after"}, f"{item['id']}: bad phase")
        elif kind == "http_response":
            exact_keys(assertion, {"target", "path", "op", "value"}, f"{item['id']}.assertion")
            require(assertion["target"] in {"action_origin", "status", "field"}, f"{item['id']}: bad target")
            require(assertion["target"] == "field" or assertion["path"] == "", f"{item['id']}: scalar target path")
        else:
            exact_keys(assertion, {"path", "op", "value"}, f"{item['id']}.assertion")
        check_pointer(assertion["path"], item["id"], empty_ok=kind == "http_response")
        require(assertion["op"] in OPS, f"{item['id']}: open operator")
        encoded = json.dumps(assertion, sort_keys=True, separators=(",", ":"))
        require(encoded not in seen_assertions, f"{item['id']}: duplicate assertion")
        seen_assertions.add(encoded)

    observed = synthesized_observed(predicate)
    require(evaluate(predicate, observed), f"{item['id']}: synthesized positive did not pass")
    negative_count = 0
    for assertion in assertions:
        key = assertion_key(kind, assertion)
        mutated = copy.deepcopy(observed)
        mutated.pop(key)
        require(not evaluate(predicate, mutated), f"{item['id']}: missing-field negative passed")
        negative_count += 1
        mutated = copy.deepcopy(observed)
        mutated[key] = incompatible_value(assertion)
        require(not evaluate(predicate, mutated), f"{item['id']}: wrong-value negative passed")
        negative_count += 1
    return negative_count


def check_request(item: dict[str, Any]) -> None:
    request = item["executable_request"]
    exact_keys(request, {"schema_version", "kind", "context_bindings", "recorder"}, f"{item['id']}.request")
    require(request["schema_version"] == 1, f"{item['id']}: request schema")
    require(request["context_bindings"] == CONTEXT_BINDINGS, f"{item['id']}: context bindings")
    predicate = item["canonical_predicate"]
    require(request["kind"] == predicate["kind"], f"{item['id']}: kind mismatch")
    recorder = request["recorder"]
    require(recorder["source_id"] == predicate["source_id"], f"{item['id']}: source mismatch")
    kind = predicate["kind"]
    if kind == "state_transition":
        exact_keys(recorder, {"source_id", "before", "action", "after"}, f"{item['id']}.recorder")
        recipe = {
            "before": recorder["before"],
            "actions": recorder["action"],
            "after": recorder["after"],
        }
        adapter_recipe = item["adapter"]["recipe"]
        require(
            recipe == {key: adapter_recipe[key] for key in recipe},
            f"{item['id']}: adapter/recorder recipe drift",
        )
        available: dict[str, set[str]] = {}
        for phase in ("before", "after"):
            require(isinstance(recorder[phase], list) and recorder[phase], f"{item['id']}: empty {phase}")
            available[phase] = set()
            for selection in recorder[phase]:
                exact_keys(selection, {"path", "selector", "property"}, f"{item['id']}.{phase}")
                check_pointer(selection["path"], item["id"])
                properties = {"text", "value", "count", "disabled", "checked", "hidden", "class"}
                require(
                    selection["property"] in properties,
                    f"{item['id']}: property",
                )
                require(isinstance(selection["selector"], str) and selection["selector"], f"{item['id']}: selector")
                available[phase].add(selection["path"])
        available["action"] = set()
        require(isinstance(recorder["action"], list) and recorder["action"], f"{item['id']}: empty action")
        for action in recorder["action"]:
            action_keys = {
                "observe": {"kind", "path"},
                "click": {"kind", "selector", "path"},
                "set_value": {"kind", "selector", "value", "path"},
                "select": {"kind", "selector", "value", "path"},
                "submit": {"kind", "selector", "path"},
                "press_key": {"kind", "selector", "key", "path"},
                "wait": {"kind", "milliseconds", "path"},
                "fetch": {"kind", "method", "endpoint", "body", "path"},
                "fetch_json": {
                    "kind", "method", "endpoint", "body", "pointer", "path",
                },
                "assistant_binding": {
                    "kind", "endpoint", "assistant_id", "path",
                },
                "click_response_json": {
                    "kind", "selector", "method", "endpoint", "pointer", "path",
                },
            }
            kind_name = action.get("kind")
            require(kind_name in action_keys, f"{item['id']}: action kind")
            exact_keys(action, action_keys[kind_name], f"{item['id']}.action")
            check_pointer(action.get("path"), item["id"])
            if kind_name in {
                "click", "set_value", "select", "submit", "press_key",
                "click_response_json",
            }:
                require(
                    isinstance(action["selector"], str) and action["selector"],
                    f"{item['id']}: action selector",
                )
            if kind_name in {"fetch", "fetch_json", "click_response_json"}:
                require(
                    action["method"] in {"GET", "POST"}
                    and isinstance(action["endpoint"], str)
                    and action["endpoint"].startswith("/")
                    and not action["endpoint"].startswith("//")
                    and "#" not in action["endpoint"],
                    f"{item['id']}: response capture target",
                )
                if kind_name in {"fetch", "fetch_json"}:
                    require(
                        (action["method"] == "GET" and action["body"] is None)
                        or (
                            action["method"] == "POST"
                            and isinstance(action["body"], dict)
                        ),
                        f"{item['id']}: browser HTTP method/body mismatch",
                    )
            if kind_name in {"fetch_json", "click_response_json"}:
                check_pointer(action["pointer"], item["id"])
            if kind_name == "assistant_binding":
                require(
                    action["endpoint"] == "/api/extensions/assistants"
                    and isinstance(action["assistant_id"], str)
                    and re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                        action["assistant_id"],
                    ) is not None,
                    f"{item['id']}: assistant binding action",
                )
            available["action"].add(action["path"])
        for assertion in predicate["assertions"]:
            require(assertion["path"] in available[assertion["phase"]], f"{item['id']}: unrecorded assertion path")
    elif kind == "http_response":
        exact_keys(recorder, {"source_id", "action_origin", "request"}, f"{item['id']}.recorder")
        http = recorder["request"]
        exact_keys(http, {"method", "path", "body", "select"}, f"{item['id']}.http")
        require(
            http["method"] in {"GET", "POST"} and http["path"].startswith("/"),
            f"{item['id']}: HTTP target",
        )
        require(
            (http["method"] == "GET" and http["body"] is None)
            or (http["method"] == "POST" and isinstance(http["body"], dict)),
            f"{item['id']}: HTTP method/body mismatch",
        )
        require(item["adapter"]["method"] == http["method"], f"{item['id']}: adapter method drift")
        require(item["adapter"]["path"] == http["path"], f"{item['id']}: adapter path drift")
        selected = set(http["select"])
        for pointer in selected:
            check_pointer(pointer, item["id"])
        fields = {a["path"] for a in predicate["assertions"] if a["target"] == "field"}
        require(fields == selected, f"{item['id']}: HTTP selection/predicate mismatch")
        require(item["adapter"]["select_allowlist"] == http["select"], f"{item['id']}: HTTP adapter selection drift")
    else:
        exact_keys(recorder, {"source_id", "tool", "arguments", "select"}, f"{item['id']}.recorder")
        require(isinstance(recorder["tool"], str) and recorder["tool"], f"{item['id']}: MCP tool")
        require(recorder["arguments"] == {}, f"{item['id']}: unbounded MCP arguments")
        expected_select = [assertion["path"] for assertion in predicate["assertions"]]
        require(
            recorder["select"] == expected_select,
            f"{item['id']}: MCP selection/predicate mismatch",
        )
        require(item["adapter"]["select_allowlist"] == expected_select, f"{item['id']}: MCP adapter selection drift")
        for pointer in expected_select:
            check_pointer(pointer, item["id"])


def assertions_by_id(proposals: list[dict[str, Any]], ticket_id: str) -> list[dict[str, Any]]:
    return next(item for item in proposals if item["id"] == ticket_id)["canonical_predicate"]["assertions"]


def require_assertion(proposals: list[dict[str, Any]], ticket_id: str, **expected: Any) -> None:
    assertions = assertions_by_id(proposals, ticket_id)
    found = any(
        all(item.get(key) == value for key, value in expected.items())
        for item in assertions
    )
    require(found, f"{ticket_id}: missing {expected}")


def check_regressions(proposals: list[dict[str, Any]]) -> None:
    registration = next(
        item for item in proposals
        if item["id"] == "extension.environment-free-mcp-registration"
    )
    env = assertions_by_id(proposals, "extension.environment-free-mcp-registration")
    require(len(env) == 3, "environment-free registration needs three closed browser assertions")
    require(
        registration["executable_request"]["kind"] == "state_transition"
        and registration["adapter"]["adapter"] == "trusted_browser_state_v1",
        "environment-free registration must be browser-observed",
    )
    require_assertion(
        proposals, "extension.environment-free-mcp-registration", phase="action",
        path="/mcp_transport", op="eq",
        value={
            "type": "stdio", "command": "pursers-wait-bridge", "args": [], "env": {},
        },
    )
    require_assertion(
        proposals, "extension.environment-free-mcp-registration", phase="after",
        path="/registration_status", op="contains",
        value="Registration recovered without replaying the door.",
    )
    require_assertion(
        proposals, "fleet.refresh-pause-resume", phase="before",
        path="/paused_status", op="contains", value="Refresh paused while editing",
    )
    require_assertion(
        proposals, "fleet.refresh-pause-resume", phase="action",
        path="/resume_action", op="eq", value="clicked",
    )
    require_assertion(
        proposals, "fleet.refresh-pause-resume", phase="after",
        path="/resumed_status", op="contains", value="Updated",
    )
    require_assertion(proposals, "fleet.doors-expiry", phase="after", path="/issued_expiry", op="ne", value="—")
    require_assertion(proposals, "fleet.doors-key-id", phase="after", path="/issued_kid", op="contains", value="kid-")
    require_assertion(
        proposals, "fleet.doors-connected-seats", phase="after",
        path="/connected_seat_count", op="eq", value=2,
    )
    require_assertion(
        proposals, "personal.fleet-shared-pool", phase="after",
        path="/shared_pool_row", op="contains", value="worker-three",
    )
    require_assertion(
        proposals, "personal.today-active-agents", phase="before",
        path="/empty_team_count", op="eq", value=1,
    )
    require_assertion(
        proposals, "personal.today-active-agents", phase="after",
        path="/active_agent_count", op="eq", value=4,
    )
    require_assertion(
        proposals, "personal.today-current-work", phase="before",
        path="/empty_work_count", op="eq", value=1,
    )
    require_assertion(
        proposals, "personal.today-current-work", phase="after",
        path="/active_work_count", op="eq", value=4,
    )
    require_assertion(
        proposals, "personal.work-ownership", phase="after",
        path="/owner_metadata", op="contains", value="owner worker-three",
    )
    require_assertion(proposals, "personal.source-board-snapshot", path="/board/id", op="eq", value="sandbox-board")
    require_assertion(proposals, "personal.source-link-snapshot", path="/source_tool", op="eq", value="memory_links")
    require_assertion(
        proposals, "personal-mcp.state.fleet-unavailable", path="/registry_warning",
        op="eq", value="unavailable active boards: sandbox-unavailable",
    )


def validate(delta: dict[str, Any]) -> tuple[int, int, int]:
    exact_keys(delta, set(delta["proposal_contract"]["top_level_keys"]), "delta")
    check_partition(delta)
    proposals = delta["proposals"]
    executable = [item for item in proposals if item["status"] == "executable_proposal"]
    gaps = [item for item in proposals if item["status"] == "contract_gap"]
    gap_ids = {item["id"] for item in gaps}
    require(
        len(executable) == 119 and gap_ids == EXPECTED_CONTRACT_GAPS,
        "expected 119 executable and the two unresolved gaps",
    )
    gap_summaries = delta["contract_gaps"]
    require(
        len(gap_summaries) == 2
        and {item["id"] for item in gap_summaries} == EXPECTED_CONTRACT_GAPS,
        "top-level contract-gap summary drift",
    )
    normal_keys = set(delta["proposal_contract"]["executable_proposal_keys"])
    gap_keys = set(delta["proposal_contract"]["contract_gap_keys"])
    for item in gaps:
        check_contract_gap(item, gap_keys)
        summary = next(row for row in gap_summaries if row["id"] == item["id"])
        exact_keys(
            summary,
            {
                "id", "missing_channel_or_operation",
                "bounded_parent_correction", "owner",
            },
            f"{item['id']}.gap_summary",
        )
        require(
            summary["missing_channel_or_operation"]
            == item["adapter"]["missing_channel_or_operation"]
            and summary["bounded_parent_correction"]
            == item["adapter"]["bounded_parent_correction"]
            and summary["owner"]
            == "TK-a565b6635db1 parent shared adapter/producer",
            f"{item['id']}: gap summary mismatch",
        )
    semantic: set[str] = set()
    negative_cases = 0
    first_negatives: set[str] = set()
    for item in executable:
        exact_keys(item, normal_keys, item["id"])
        require(item["adapter"]["support_status"] == "supported", f"{item['id']}: support status")
        require(item["validation"]["executable_now"] is True, f"{item['id']}: executable marker")
        negatives = item["validation"]["adversarial_negatives"]
        require(
            negatives
            and negatives[0]["mutation"] == item["causal"]["negative_counterexample"],
            f"{item['id']}: fact-specific negative",
        )
        require(negatives[0]["mutation"] not in first_negatives, f"{item['id']}: duplicate fact-specific negative")
        first_negatives.add(negatives[0]["mutation"])
        check_request(item)
        negative_cases += check_predicate(item)
        normalized = normalized_predicate(item["canonical_predicate"])
        require(normalized not in semantic, f"{item['id']}: duplicate semantic predicate")
        semantic.add(normalized)
    require(len(semantic) == 119, "semantic predicates must be unique without source_id")
    check_regressions(proposals)
    negative_cases += check_fact_specific_mutations(proposals)
    producer_examples = (
        check_parent_producer_examples(proposals)
        + check_assistant_binding_examples(proposals)
    )
    return check_source_anchors(proposals), negative_cases, producer_examples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("delta", nargs="?", type=Path, default=DEFAULT_DELTA)
    args = parser.parse_args()
    try:
        delta = json.loads(args.delta.read_text())
        anchors, negatives, producer_examples = validate(delta)
    except (Invalid, KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"predicate_proposals=FAIL: {error}", file=sys.stderr)
        return 1
    print(f"proposal_file={args.delta.relative_to(ROOT) if args.delta.is_relative_to(ROOT) else args.delta}")
    print("owned=121 complement=43 executable=119 contract_gaps=2")
    print("unique_semantic_predicates=119")
    print(f"validated_source_anchors={anchors}")
    print(f"schema_positive_cases=119 negative_cases={negatives}")
    print(f"actual_parent_producer_examples={producer_examples}")
    print("predicate_proposals=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
