# Butler-owned VM Workflows Implementation Plan

> **For fleet workers:** Execute through Butler-managed Pursers tickets and
> independent review. Do not spawn host subagents. The operator owns the final
> integration gate, generated artifacts and immutable deployment.

**Goal:** Make resident Butler advance both human-coordinator and autonomous
Sonar work through dependency gates, review, final validation and configured PR
delivery without a second scheduler.

**Architecture:** Retain the existing Central board, registry, native Butler
resident loop, approved fleet executor and delivery runtime. Normalize both
origins into a durable, operator-activated project workflow. The Coordinator
defines scope and answers genuinely missing project decisions; Butler owns
normal execution and delivery. A skill file alone is not runtime implementation.

**Tech Stack:** Python, existing MCP/Central APIs, signed fleet executor,
versioned project policy/state, Git and existing batch delivery adapter.

**Spec:** Operator-approved `butler_autonomy_contract_v1` board state. Fleet
copies are supplied as `.operator-context/butler-autonomy-contract-v1.json`
within the registered project and in ticket annotations, without broadening
worker access to arbitrary board state.

## Global constraints

- Preserve the private 5.2.1 cohort and both existing intake routes; no version
  bump or public upstream push.
- Butler retains false work/review capabilities. Its existing administrative
  membership and `board:read`, `board:write`, `board:coordinate` scopes are not
  an independent-review identity or permission to raise its own envelope.
- Enforce registered project/board/repository/branch boundaries, active policy,
  capacity, provider health, budgets, live leases and approved templates.
- Customer environment branches and final customer merge remain manual.
  Reviewed integration happens on a configured owned branch.
- No routine paid empty-queue decision, full-board prompt or blind replay.
- Inspect baseline source, configuration, routes, API clients, tests and prior
  project decisions before escalating. Preserve existing authentication during
  an unrelated dependency upgrade. Specific registration/callback facts become
  human questions only if necessary for the change and still unavailable;
  generic documentation does not prove the customer's production contract.
- Validation is scoped per ticket and whole-project at final QA. Disclose
  unchanged unrelated baseline failures; reject unfixed targets and introduced
  regressions. Do not claim false green or weaken independent review.

## Review focus

1. Source and coordinator tickets must enroll with complete, sealed membership;
   the first completed member must not prematurely publish the project.
2. Changed source SHA, incomplete reads or same-principal review must block
   dependent release without blocking unrelated projects.
3. Existing human questions must not become implicit approval because a model
   or a generic skill can propose an answer.
4. Reconnect, restart, CAS races and unknown remote outcomes must not duplicate
   QA tickets, integration or PR publication.
5. Child exit status is not ticket completion; diagnostics must preserve useful
   failure evidence while keeping secrets out of shared output.

## Task 1: Native resident workflow progression

**Fleet ticket:** `TK-3dc28fcc300e08428fd6`.

The initial ticket was superseded after bounded continuation exhaustion;
`TK-eafc4984614b64d24c9a` owns the focused remaining native wiring. The original
checkpoint, working diff and history are preserved. This transition does not
mark the incomplete code accepted or any original EOL scope complete.

**Files:** `tools/board-butler/board_butler.py`, focused workflow module/tests,
existing integration/delivery policy seams and new operation/config examples.
Do not concurrently edit event-seat diagnostics or the delivery wrapper owned
by Task 3. Task 1 owns the final native wiring; Task 4 owns the real Central
end-to-end tests and Task 5 owns the isolated delivery-runtime binding.

**Consumes:** Approved registry/project policy and explicit workflow/cohort
record, complete ticket projections, exact accepted source/review evidence,
existing executor and integration/delivery adapters.

**Produces:** Native resident evaluation, guarded/idempotent dependency release,
one configured final-validation ticket, and enrollment/publication for either
origin using the existing configured delivery contract.

- [ ] Reproduce the parked coordinator-ticket stall with a disposable real
  Central before adding the resident capability.
- [ ] Define and validate the small versioned workflow/activation contract;
  reject malformed, cyclic, cross-project and unauthorized records.
- [ ] Connect evaluation and supported mutations to the resident's existing
  active loop; no coordinator sidecar or additional Butler process.
- [ ] Test independent exact-SHA prerequisite acceptance, pending domain holds,
  changed evidence, partial reads, duplicate/restart/CAS behavior and isolated
  per-project failure.
- [ ] Enroll both intake origins and sealed cohorts. Exercise one final-QA task
  and one correlated configured delivery outcome on the exact combined source.
- [ ] Add the scoped-review regression: baseline 10 findings, requested finding
  fixed, unrelated 9 unchanged and no new regression is eligible for scoped
  review; an unfixed target/new defect fails. Final QA retains its configured
  full-project gate and explicit baseline disposition.
- [ ] Run affected suites, document configuration/authority/activation, and
  submit an exact tested cumulative branch/SHA for independent review.

## Task 2: Runner diagnostics and bounded recovery evidence

**Fleet ticket:** `TK-69a476e1ecf86f78b6c0`.

**Files:** `tools/seat-kit/event_seat.py`, a focused diagnostics helper if
needed, seat-kit tests and runner operations documentation. Do not change the
workflow owner's reconciliation code.

**Consumes:** Actual child process result, exact current ticket/claim identity,
existing bounded continuation state and private runtime paths.

**Produces:** Versioned compact outcome/evidence reference, redacted owner-only
diagnostics, and truthful terminal/retry classification. Scheduling remains
Butler's responsibility.

- [ ] Reproduce lost nonzero/timeout diagnostics and exit-zero without a real
  submission using subprocess fakes, without a paid provider call.
- [ ] Retain bounded private evidence with correlation, omission/retention
  metadata and secret redaction before shared output.
- [ ] Preserve role/principal/lease/positive-cursor/drain fences and existing
  continuation bounds across restart and replay. Distinguish a fresh authorized
  attempt from identical failed input; do not buy retries by resetting state.
- [ ] Coordinate the additive outcome/review packet interface through ticket
  annotations. Where the event-seat review prompt is the boundary, pass the
  explicit scoped-versus-final acceptance cue without weakening source proof.
- [ ] Run the affected seat-kit suite and submit the exact cumulative tested
  branch/SHA for independent review.

## Task 3: Reviewed-cohort delivery adapter

**Fleet ticket:** `TK-f59ac5721974a1ee022b`.

**Files:** `tools/board-butler/workflow_delivery.py`, its uniquely named tests
and focused delivery documentation. Do not edit Task 1's resident loop or
Task 2's runner files.

Reuse `BatchDeliveryRuntime.collect()` to prepare the actual merged owned
branch without a release request. The existing `ready` trigger withholds
publication until an explicit complete cohort request. After independent QA
accepts the exact combined source and manifest, reuse `release()` with that
cohort identity and membership. Retain the existing ledger, verified Git and
approved connector; do not create another merge engine or scheduler.

The agreed async entry points are `prepare_workflow_delivery(runtime,
delivery_policy, workflow_policy, members)` and
`publish_workflow_delivery(runtime, delivery_policy, workflow_policy, qa_ticket,
prepared)`. A prepared result includes `state`, `combined_revision`,
`member_manifest`, `batch_key`, `cohort_id`, `workflow_policy_sha256` and
`delivery_policy_revision`; blocked results retain their reason. Preserve
actual test and baseline evidence instead of manufacturing successful flags.

- [ ] Test two parallel accepted branches, no premature publication, exact QA,
  both origins, replay/restart, head/member/policy drift, same-principal review
  and unknown remote outcomes using temporary Git and fake connectors.
- [ ] Submit the exact tested adapter for independent review. Publish its
  return contract and accepted SHA to Task 1.
- [ ] Task 1 may incorporate that independently approved commit on its own
  candidate branch, preserving ancestry and reporting the full cumulative
  source. Run the real disposable-Central dual-flow gate on that composition.

## Operator integration and activation

### Dedicated test and runtime-binding work

Task 4, `TK-e664c0e0e550f18fbb71`, owns only
`tools/board-butler/tests/test_native_project_workflow_e2e.py` and uniquely named
fixture helpers. It uses disposable real Central instances, separate work and
review identities, temporary Git repositories and a fake external PR connector.
It owns the coordinator-origin case through dependency release, actual combined
source, readable final QA, one PR and restart recovery. The source-origin case
was split into `TK-c1bd02ecde45e69d4920`, which owns only
`test_native_source_workflow_e2e.py` and unique fixtures. That case must use the
real managed intake poller, intake approval/materialization, non-home board
routing and retained source index. Replacing the controller,
Central mutations, Git integration or QA gates with a successful fake is not an
end-to-end test.

Task 5, `TK-6f30cd39894be71a8586`, owns only a new
`tools/board-butler/workflow_runtime.py`, its focused tests and binding guide.
Its async `build_workflow_runtime(backend, project)` returns the existing
`BatchDeliveryRuntime` and compiled delivery policy. Native Task 1 calls it from
`CentralBackend._workflow_delivery_runtime(project)`. The binding uses the exact
registered project, approved connector, protected fleet clone and durable
project-specific ledger. A coordinator-only project must not require a Sonar
source or a fabricated issue ID. Missing or ambiguous authority blocks delivery;
it does not broaden credentials or skip existing mutation checks.

Specific test/helper checkpoints may be composed on development branches so
owners can test the actual joint source. A red checkpoint is evidence of work in
progress, not permission to deploy. Independent review and the final integration
gate must cover the exact cumulative candidate.

The pending-head adapter correction, `TK-6a1d470efbcb02248d98`, permits an
explicit null combined revision during preparation. Preparation derives the
actual merged head; publication consumes the same sealed input policy and exact
prepared identity. Do not predict the merge SHA or reseal a different policy
after preparation.

The Central attestation correction, `TK-57dee5511bbce5737c27`, uses the existing
immutable submission and review histories. `workflow_delivery_attestation()`
returns one `workflow_delivery_sha256: <digest>` line binding the full prepared
identity. The QA worker records it in submission notes after validation; the
independent reviewer records the same marker in review notes after verification.
Both existing typed commit fields must identify the exact combined SHA. Native
wiring must preserve the real records; synthetic verdict fields cannot establish
approval. Full evidence remains available through the framed, paged packet.

### Verified checkpoints, 2026-10-25

- Task 2 independently accepted `567e197becd5fccad002f61404d45ed03480dfe1`.
  Operator verification on the current VM interpreter: 357 passed, no failures.
- Task 3 independently accepted `11cba0b9fcc2fa40bc975b07c755b1da2303657c`.
  Independent focused verification: 104 passed.
- The runtime binding independently accepted
  `5becb56be580c5c7493a15315800169bd458222a`; the pending-head correction accepted
  `e5d5002bf821b4fbcc31c6390ad693e2119b79fd`.
- The real Central attestation adapter independently accepted
  `b119965d0c2a62719c2c41e67316f893f47fb108`; its focused suite passed 12 tests.
  These are component approvals. The native controller and both end-to-end
  cases still require a final cumulative review.
- Native Task 1 and the combined end-to-end work remain incomplete. These
  checkpoints do not establish deployment or completion of either live flow.
- The EOL requirement file was mapped to all nine original tickets. Explicit
  amendments assign the six-row VA/risk/RA report, shared dependency consumers
  and scoped-versus-final acceptance. Unknown VA results remain unknown. The
  conditional final QA scope remains additional validation, not a missing tenth
  implementation ticket.

- [ ] Fetch and verify all independently approved branch tips; compose them
  with these role-contract updates in the private fork.
- [ ] Regenerate release-owned artifacts with the supported generators and run
  the full fourteen-suite gate once on the frozen integrated candidate.
- [ ] Build the immutable private cohort; back up consistent SQLite, config,
  workflow/index/cursors and executor state. Preserve active work/review leases
  and retain rollback. No live worker source patch or credential copying.
- [ ] Deploy through the stable compatibility prefix, then explicitly activate
  the reviewed workflow capability/policy for the intended project.
- [ ] Verify the registered protected clone and explicitly configure fleet
  ownership, batch delivery and matching workflow/delivery activation pins.
  Preserve existing independent-review and upstream-policy requirements. Use
  the project's supported Node/package-manager executable through argv-only
  validation commands; a shell assignment is not an executable argument.
- [ ] Normalize the existing EOL canary record: nine original tickets, eight
  dependency-parked nodes and one accepted foundation. Preserve all checkpoints
  and the conditional final-validation scope; do not recreate the initial nine.
- [ ] Verify actual Butler-driven dependent release, independent review,
  combined QA and configured owned-branch publication. Report these as separate
  facts; do not infer deployment from a passing test or saved draft.

The initial nine EOL tickets already existed. The tenth was deliberately a
conditional final-validation draft. Two unrelated obsolete tickets were canceled
through supported coordinator operations, with the previous administrative
decision preserved. These repairs do not claim that EOL delivery is complete.
