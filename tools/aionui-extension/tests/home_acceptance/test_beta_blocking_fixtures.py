from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .beta_blocking_fixtures import BOARD_ID, FIXTURE_DIR, load_fixture, materialize_door


REPO = Path(__file__).resolve().parents[4]
PLAN_COMMIT = "d5e98bc09f2fb86b640402d44824ef1795ecd0a8"
PLAN_SHA256 = "8a5b418d340d1bb30df4d39ddc9cf2b10022f1e2c8e323a2b36c9215e3b8b365"
RECIPE_ROWS = {
    14: "browser-extension-join-state-joined",
    18: "browser-extension-join-form",
    20: "browser-extension-redacted-status-card",
    40: "browser-fleet-board-cards",
    70: "browser-fleet-hub-overview",
    95: "browser-fleet-ticket-counts",
    97: "browser-fleet-updated-state",
    133: "browser-personal-keyboard-tabs",
}
REPORT_DIR = Path(__file__).with_name("reports") / "beta_blocking"


def test_approved_split_and_recipe_rows_are_exact() -> None:
    split = REPO / "planning" / "beta-ga-split.json"
    assert hashlib.sha256(split.read_bytes()).hexdigest() == PLAN_SHA256
    plan = json.loads((REPO / "planning" / "gap-implementability-map.json").read_text())
    found = {row["index"]: row for row in plan["rows"] if row["index"] in RECIPE_ROWS}
    assert {index: row["canonical_predicate"]["source_id"] for index, row in found.items()} == RECIPE_ROWS
    assert all(row["trust_source_recipe_fragment"]["recipe"] for row in found.values())


def test_synthetic_fixture_rows() -> None:
    worker = load_fixture("valid-worker-door")
    reviewer = load_fixture("valid-reviewer-door")
    assert (worker["board_id"], worker["role"]) == (BOARD_ID, "worker")
    assert (reviewer["board_id"], reviewer["role"]) == (BOARD_ID, "reviewer")
    assert load_fixture("extension-status-loaded")["onboarding_status"]["seats"][0]["board"] == BOARD_ID
    board = load_fixture("personal-board-empty")
    assert board["board"]["id"] == BOARD_ID and board["ticket_total"] == 0 and board["tickets"] == []
    assert load_fixture("personal-board-identity")["expected_text"] == BOARD_ID
    assert len(load_fixture("personal-fleet-projects")["projects"]) >= 1


def test_each_recipe_has_an_honest_not_final_report() -> None:
    reports = [json.loads(path.read_text()) for path in sorted(REPORT_DIR.glob("row-*.json"))]
    assert [report["row_index"] for report in reports] == sorted(RECIPE_ROWS)
    assert all(report["label"] == "NOT final" for report in reports)
    assert all(report["source_id"] == RECIPE_ROWS[report["row_index"]] for report in reports)
    assert all(report["status"] == "blocked_before_navigation" and not report["passed"] for report in reports)
    assert all(report["route_candidate_changes_to"] == "TK-4981bc2da3ac" for report in reports)


def test_disposable_doors_pass_shipped_parser_without_persisting_credentials() -> None:
    adapter = REPO / "tools" / "aionui-extension" / "door" / "adapter.cjs"
    script = """
const adapter = require(process.argv[1]).createDoorOnboarding({
  nowEpoch: () => 1900000000,
  runBridge: async () => [],
  importMcp: async () => ({ success: true }),
});
const rows = JSON.parse(process.argv[2]);
for (const row of rows) {
  const result = adapter.parse(row.door);
  if (!result.ok || result.metadata.board !== row.board || result.metadata.role !== row.role) process.exit(2);
}
"""
    rows = [
        {"door": materialize_door("valid-worker-door", 49101), "board": BOARD_ID, "role": "worker"},
        {"door": materialize_door("valid-reviewer-door", 49102), "board": BOARD_ID, "role": "reviewer"},
    ]
    subprocess.run(["node", "-e", script, str(adapter), json.dumps(rows)], check=True, capture_output=True, text=True)
    assert all("prs1." not in path.read_text() for path in FIXTURE_DIR.glob("*.json"))
