# Resident Butler project workflows

Project workflows are opt-in, versioned contracts in the exact routed
`project_registry` entry. They are shared by coordinator-created and Sonar
source-intake tickets: origin is audit context, not a separate scheduler.
Unknown, incomplete, cross-board/project, unsealed, cyclic, or inactive workflow
state does not affect other registered projects. Existing delivery behavior is
not migrated by adding a policy draft.

## Contract and activation

`butler_workflow` has schema version 1 and the exact fields below. The ordered
`nodes` list is the durable sealed cohort manifest; each node names an existing
board ticket, prerequisite node IDs, and nonempty acceptance checks. Node and
ticket IDs are unique. Every prerequisite must be in the manifest and the graph
must be acyclic. `final_validation` contains a title and meaningful checks.

```json
{
  "schema_version": 1,
  "workflow_id": "workflow:sample",
  "project_id": "sample",
  "board_id": "sample-board",
  "cohort_id": "cohort:2026-01",
  "sealed": true,
  "nodes": [{
    "node_id": "foundation",
    "ticket_id": "TK-existing",
    "requires": [],
    "acceptance_checks": ["Declared scope independently reviewed at exact SHA"]
  }],
  "final_validation": {
    "title": "Validate the sealed cohort",
    "acceptance_checks": ["Run configured full-project checks on the exact combined revision"]
  }
}
```

Activation is a separate explicit project setting and pins the normalized,
complete workflow document. Compute `policy_sha256` as lowercase SHA-256 of
UTF-8 canonical JSON (`sort_keys=true`, separators `(',', ':')`,
`ensure_ascii=false`) for the validated normalized policy. It includes the
project and board route, seal, all cohort nodes and checks, and final task
contract. The activation document has exactly these fields:

```json
{"schema_version":1,"state":"active","policy_sha256":"<64 lowercase hex>","activation_id":"operator:change-reference"}
```

Only the project owner/operator activates the reviewed draft. A policy edit
invalidates its previous activation. Do not use a generic `active` flag as a
substitute for the digest pin.

## Progression and safety

On an active-mode resident refresh, Butler reads each declared ticket's full
projection. An incomplete manifest projection blocks that workflow's actions.
A dependency is eligible for unpark only when every prerequisite has a closed
submission and independent approval by a different principal on the exact
currently submitted SHA. A pending human request, live holder/review lease, or
changed/missing review evidence blocks release. A current exact-SHA independent ticket review establishes acceptance of that
ticket's declared scope; it does not assert that the whole project passed. Preserve
the submission and review reports, including disclosed wider baseline failures,
verbatim as evidence. Butler does not reinterpret opaque reports as a blanket
second project gate. Final combined QA still must satisfy the configured
whole-project checks, and a failed final report blocks publication. Release uses
Central's native `ticket_update(parked=false)`;
replay is a harmless repeat, and it never resolves a human request.

After every declared member has exact-SHA independent approval, Butler may
materialize one deterministic final-validation ticket. The bounded description
contains the exact combined revision and an index (SHA-256 plus chunk count) to
the complete immutable QA packet. Ordered `kind=evidence` annotations contain
the packet bytes: full project/board/workflow/cohort identity, combined SHA,
complete member-to-SHA manifest, configured acceptance checks, and manual
customer-merge boundary. The page size respects Central's 4,000-character
annotation limit; the description respects its 5,000-character limit. Packet
JSON escapes spaces and Unicode before paging so Central's annotation text
normalization cannot trim exact report bytes at page boundaries. If the
complete policy/evidence cannot fit within retained ticket evidence capacity,
creation fails closed before creating a QA ticket. Readers verify the ordered
pages against the packet digest. Deterministic identity and full-ticket reads
make retries/restarts idempotent; interrupted annotation paging is reconciled
without creating another task. Missing repository identity, a non-fleet
checkout, absent accepted object, or no single revision containing all
accepted candidates blocks final QA creation.

Final-QA worker workspaces are ticket-bound to `pursers/<final-ticket-id>` and
start at the exact `combined_revision` from this activated, immutable packet;
they never begin from `origin/main`. Event preflight validates the authenticated
registry route, sealed active workflow, deterministic ticket ID, complete
packet pages and protected-clone Git object before provisioning. A clean legacy
QA worktree detached at that exact SHA can be attached to its deterministic
branch only during authorized worker provisioning. If an unused deterministic
branch already points to that SHA or a verified ancestor, the worker fast-forwards
it with a compare-and-swap Git ref update; this recovers legacy pre-created refs
without resetting the worktree or changing unrelated refs. Read-only Butler
evidence reports detached state truthfully and checks branch ownership and
ancestry without mutation. Wrong SHA, dirty/foreign/ambiguous worktrees, divergent
or elsewhere-attached branches and ordinary detached worker contexts remain
refused. Reviewer workspaces remain independently read-only and bound to the
exact submitted candidate.

After independent approval of the exact final-QA source, native reconciliation
requires the immutable prepared-workflow digest in the actual QA submission and
independent review records before invoking the existing approved delivery
runtime. The same sealed pending-head policy and prepared identity are used for
publication; replay is constrained by the delivery ledger and exact source/PR
correlation. This applies to both coordinator-origin and source-origin cohorts;
active workflow-owned source intake defers legacy per-ticket writeback until
native QA/publication, while unconfigured projects retain their existing
behavior. A final-QA pass alone does not authorize or perform delivery.
Configured customer final merge remains manual. Do not interpret member scope
acceptance as whole-project success: strict configured combined validation and
final QA evidence still gate publication by default. An operator may opt an
individual `project_registry.projects.<name>` row into
`final_validation_delivery: "draft_with_report"` through the canonical
`project_registry` state API with compare-and-swap and read-back; only an
existing board admin can change this field. The default `"require_pass"` is
strict and unchanged. The opt-in may create one correlated **draft** PR with a
truthful report for a completed independent approve or reject review of the
exact prepared revision, including a report-held security ticket; missing,
stale, self-reviewed, mismatched, or incomplete evidence still blocks. This
preference is separate from activation, sealed packet, and delivery-policy
identity. A report-held draft does not close or approve QA, pass final
validation, resolve security holds, mark the PR ready, or authorize promotion,
merge, autocomplete, or deployment. VA may remain UNKNOWN and RA NOT APPROVED;
the customer team retains the final manual merge boundary. Strict ready-PR
publication retains its existing positive QA and cumulative-validation gates.

Customer PR bodies (ready, report-held draft, and rolling updates) are bounded
at 3,700 UTF-16 code units before a snapshot/create/update reservation is written.
They carry the immutable combined revision and member-manifest digest plus the
exact full QA ticket/policy reference when available. Complete member evidence
and all QA report fields remain in the private canonical batch ledger and QA
packet; no prose or evidence is sliced to fit the provider description limit.
Unknown create outcomes remain reconcile-only, and a confirmed held-draft state
is persisted before return so a restarted resident can resume safely.

This contract does not grant work or review authority, waive failed tests, interpret
human decisions, write customer base branches, force push, deploy, or merge a
customer PR. Independent reviewer identity and exact source evidence remain
required. Configure publication only through the project's existing approved
delivery policy, connector grants, and batch-delivery runtime; do not silently
fall back to a per-ticket or direct-write path.

## Verification and operational boundary

Run `PYTHONPATH=packages/client/src pytest -q packages/client/tests/test_project_workflow.py packages/client/tests/test_project_registry.py`
and `python3 -m pytest -q tools/board-butler/tests` (plus the project-required
coordinator and Central suites) from the repository root. Workflow unit tests
cover seals, routes, DAG/cycles, activation pinning, exact review identity,
pending human holds, release idempotence and final-task membership. A production
activation must wait for the independent code review and the complete disposable
Central, dual-intake, separate work/review-principal, exact-ref, and delivery
integration gate; a unit-test pass is not that gate.
