# Fleet seat and project lifecycle contract

Status: design report for Pursers 5.0.5. This document defines a bounded,
human-guided lifecycle. It does not implement endpoints, mutate a registry,
provision credentials, start a process, or remove files.

## Decision

The Fleet Dashboard should present one lifecycle wizard with two independently
confirmable resources: **project** and **seat**. It should orchestrate existing
primitives rather than introduce a second source of truth:

- `project_registry` remains the project routing authority.
- Central remains the authority for boards, memberships, roles, capabilities,
  leases, offers, and lifecycle status.
- Doors remain the bootstrap mechanism for worker and reviewer credentials.
- `seats.json` and managed host configuration remain the local seat inventory.
- API worker controls remain process/provider controls; they do not grant board
  membership or redefine a seat.

Every create, update, deactivate, or remove operation follows
**discover -> plan -> copy -> confirm -> apply -> verify**. Discovery and copy
are non-mutating. Apply accepts only an unexpired plan whose inputs and observed
state still match its digest. The operator may always copy generated,
secret-free configuration and finish manually before enabling automation.

The word **remove** means revoke eligibility and remove managed references. It
never means delete a project checkout, fleet clone, worktree, seat directory,
token file, key, JWKS, log, ticket, journal entry, or other durable evidence.

## Existing surface inventory

| Surface | Existing contract to reuse | Gap this design closes |
| --- | --- | --- |
| Add project | Adds schema-v1 registry entry, creates/onboards the board administrator, provisions worker/reviewer door principals, applies dispatch/review defaults, prepares a clean detached fleet clone, and returns only newly issued doors. Re-runs report already-present steps. | It applies immediately, assumes a Git-backed work directory and fleet clone, and has no pause/remove plan. |
| Doors | Lists active project/role credentials, copies a one-time `prs1` value over loopback with `no-store`, and rotates a role key after board-admin authorization. | Doors cover worker/reviewer only. A coordinator credential needs explicit administrator provisioning. Rotation is not the same as retiring one seat. |
| Seat wizard | Models host, role, identity, board selection, tier, skills, work/review flags, model/provider and credential-file references. It previews redacted diffs, applies atomic writes with backups, records inventory, returns a session prompt, and runs Doctor. | Project and membership readiness are not one transaction; plan IDs lack an explicit state digest/expiry; removal is absent. |
| Worker controls | Store provider secrets in the OS credential store and expose test/start/stop for managed API workers. | A process record is not a Central seat. Start must be gated by successful seat verification; stop must not retire membership. |
| Registry and fleet view | Reads and CAS-writes `project_registry`; shows active/paused projects, board coverage, live seats, capabilities, active/inactive status, and clone safety. | There is no guided project deactivation/removal or multi-board seat reconciliation. |
| Central lifecycle | `agent_retire` refuses an active work/review lease, revokes offers, preserves history, and creates a lifecycle transition. Rejoining with the same identity reactivates the seat. | A multi-board operator needs a single plan and per-board verification rather than best-effort retirement hidden behind one success message. |

The inventory is grounded in the current implementation: Add project and door
semantics are documented in `tools/fleet-dashboard/README.md:12-138` and
implemented in `tools/fleet-dashboard/fleet_dashboard.py:5848-6180`; seat
fields and role constraints are in `tools/fleet-dashboard/seat_config.py:611-740`;
plan/apply are in `tools/fleet-dashboard/fleet_dashboard.py:7040-7101`; registry
CAS is in `tools/fleet-dashboard/fleet_dashboard.py:4569-4652`; and Central
retirement guards are in `packages/central/src/pursers_central/central.py:7944-8010`.

## Resource model

### Project

A project lifecycle record is the existing schema-v1 registry entry plus
computed observations. The persisted values remain `board_id`, absolute
`work_dir`, `status` (`active` or `paused`), and optional `repository_url`,
`integration_ref`, and `fleet_clone_dir`. The wizard adds no secret to this
record.

The first screen accepts:

1. a unique project name and board ID;
2. an existing absolute project folder;
3. optional Git mode: `none`, `existing checkout`, or `clone from remote`;
4. for Git modes only, a repository URL and integration ref;
5. whether to prepare a separate fleet clone.

`none` registers the folder without running Git. `existing checkout` only
inspects the repository and remote; it never rewrites the remote, branch, or
working tree. `clone from remote` plans creation only into a new empty target,
uses non-interactive Git, and never pushes. Fleet clone preparation retains the
existing clean-tree/origin/ref guards. The UI must not imply that registering a
folder transfers ownership or authorizes deletion.

### Seat

A logical seat has a stable principal, agent name, role, capability declaration,
credential reference, host definition, and board membership set. One seat may
have projections on several boards. A process is merely one runtime for that
logical seat.

Required seat input:

- safe unique seat name;
- role: worker, reviewer, coordinator, or orchestrator;
- host mode: ACP host or persistent process;
- host adapter/config path;
- provider and model identifiers;
- `tier_max`, skills, `can_work`, and `can_review`;
- board selection: `registry`, one home board, or an explicit board list;
- Central URL, CA-file reference, and token-file reference;
- optional managed seat directory/repository reference.

Role constraints reuse `DesiredSeat`: worker may work but not review; reviewer
may review but not work; coordinator and orchestrator do neither. Tier is 1-3.
For a fleet-wide seat, home board is blank, registry anchor is explicit, and
`boards=registry`; a named home board is dedicated. Explicit lists are resolved
to exact board IDs at plan time.

### Host modes

**ACP host** means the host launches the connector per interactive session.
Apply writes only the host adapter's managed block and inventory record. A host
restart may be required. Closing the host makes the seat offline but does not
retire it. The generated session prompt is copyable before or after apply.

**Persistent process** means an operator-managed service or managed worker keeps
the connector available. Its plan includes service configuration plus an
explicit start/stop action. Apply never auto-starts until config, membership,
credential-reference, and Doctor checks pass. The existing API worker controls
remain a separate provider process: provider test, start, and stop do not create
or retire Central membership.

## End-to-end first-run flow

### 1. Discover

Discovery is read-only and bounded. It resolves the folder, Git state when
selected, registry SHA, target boards, current caller membership, existing
doors, matching principals/agent names, host config, seat inventory, process
state, and credential-file existence. It reads credential paths and
fingerprints, never credential contents into the browser or journal.

Discovery classifies each item as `absent`, `matching`, `conflict`, `blocked`, or
`unknown`. Unknown authorization, an incomplete board snapshot, a dirty Git
target, duplicate agent name on another principal, or ambiguous project route
blocks automation but still permits copying the manual configuration.

### 2. Plan

The server produces an immutable plan with:

```json
{
  "schema_version": 1,
  "plan_id": "opaque-random-id",
  "kind": "project-and-seat-create",
  "created_at": "2030-01-02T03:04:05Z",
  "expires_at": "2030-01-02T03:14:05Z",
  "observed_digest": "sha256:...",
  "registry_expected_sha256": "...",
  "targets": [{"board_id": "project-blue", "expected_generation": "4"}],
  "operations": [],
  "rollback": [],
  "warnings": [],
  "blocked": false
}
```

Each operation has a stable `operation_id`, authority requirement, redacted
before/after, effect class, dependencies, idempotency key, verification query,
and rollback classification (`automatic`, `manual`, or `irreversible`). The UI
shows exact filesystem paths but redacts home/account components in exported
support evidence. Secrets and authorization headers are never rendered.

Project operations reuse Add project's registry, board, door-principal, policy,
and optional clone steps. Seat operations reuse seat-config adapters,
inventory, Doctor, and persistent process controls. No operation executes while
planning.

### 3. Copy before automation

Before Confirm, provide independent copy/download actions for:

- generated host config with secret-file references only;
- the seat session prompt;
- a shell-neutral checklist for manual application;
- a redacted plan receipt;
- a one-time worker/reviewer door only after an operator explicitly requests it.

Copying config does not mark an operation applied. Door values remain
single-display, `Cache-Control: no-store`, absent from browser storage,
inventory, logs, receipts, and listing APIs. Until the CLI supports file/stdin
secret ingestion, the dashboard must not suggest pasting a door into a remote
shell command. The safe current path is an administrator-provisioned token file;
a future import action must consume a local file or protected stdin.

### 4. Confirm

Confirmation presents the plan digest, expiry, exact boards, exact files,
process actions, credential actions, and non-actions. A typed seat/project name
is required for retirement, door rotation/revocation, project pause, or managed
config removal. Credential rotation and service start are separate checkboxes,
off by default.

Confirm rechecks:

- loopback client, same-origin request, JSON content type and anti-CSRF nonce;
- the caller is administrator on every board being mutated (or has the exact
  documented coordinator authority for a permitted seat-retire call);
- registry SHA and Central generations match the plan;
- all local file digests and process observations match discovery;
- the plan is unexpired and unused.

Any mismatch invalidates the plan and returns a new diff. There is no
"continue anyway" path.

### 5. Apply

Apply serializes operations by dependency and records a non-secret receipt for
each. A retry with the same idempotency key returns `already_applied` after
verifying the intended state. It never repeats door issuance, rotates a key, or
starts a process merely because a response was lost.

Suggested create order:

1. authorize every target and reserve the plan;
2. CAS-write the project registry entry;
3. create/onboard board and reconcile memberships;
4. reconcile policies and optional fleet clone;
5. provision the seat principal/memberships;
6. atomically write managed host config and inventory with backups;
7. verify identity, memberships, and push subscription;
8. optionally start the persistent process after a second confirmation;
9. issue/display requested one-time doors last.

If a later step fails, the receipt reports `complete`, `not_started`, and
`failed` operations. It automatically restores only local files written by this
plan and only when their post-write digest is unchanged. It does not delete a
created board, registry history, clone, key, credential, or seat directory.
Remote mutations are reconciled forward or explicitly rolled back by a new
plan.

### 6. Verify

Verification is data-consuming, not expected-value synthesis. It reads actual
Central and local product state and returns per-board evidence:

- authenticated principal and agent identity match the planned seat;
- role and membership are correct on every selected board;
- advertised tier/skills/work/review/model/provider/host values match;
- `boards=registry` resolves every currently active WORK project, while explicit
  selection resolves exactly the named boards;
- registry entry and optional clone state are valid;
- token and CA files exist with safe permissions (contents remain hidden);
- a live push subscription can be established without cursor reset;
- ACP config is discoverable or persistent service is running as selected;
- Doctor result is stored in inventory with timestamp and observed board set.

Partial verification is never summarized as "ready". The result is `ready`,
`ready_offline`, `degraded`, or `blocked`, with the failed predicate and next
safe action.

## Authority and trust boundaries

| Boundary | Required authority | Forbidden shortcut |
| --- | --- | --- |
| Browser -> dashboard | Loopback, same origin, anti-CSRF nonce, JSON for mutation | Treating localhost alone as authorization |
| Dashboard -> Central | Authenticated principal with admin membership on each affected board; exact scoped coordinator exception only where Central already permits it | Inferring admin from successful status/member reads |
| Registry | Home/anchor-board admin plus CAS SHA | Last-write-wins registry overwrite |
| Local config | Operator-selected adapter and paths inside its declared managed scope | Arbitrary shell or writes outside the plan |
| Git | Read-only discovery or explicit new clone/fleet clone target | Push, remote rewrite, dirty-tree overwrite, or cleanup of existing data |
| Credentials | Token/CA path references; OS credential store for provider keys; private key files stay server-side | Secret values in browser state, logs, command lines, support exports, or config diffs |
| Process manager | Explicit start/stop permission after seat verification | Equating a running PID with valid board authority |

Coordinator credentials are never minted by worker/reviewer Doors. They are
administrator-provisioned, scoped tokens stored in a protected file. A
coordinator seat serving several boards uses one verified principal only when
that principal has an explicit membership and coordinate scope on every board;
the lifecycle plan lists and verifies each projection independently. Missing
membership on one board yields `degraded` and blocks fleet-wide activation.

## Offline, inactive, paused, and removed

These states are deliberately separate:

| State | Central membership | Local config | Process | Dispatch eligibility |
| --- | --- | --- | --- | --- |
| `online` | active | present | connected/running | according to capabilities |
| `offline` | active | present | stopped/disconnected | no live subscription; may receive an offer until Central freshness rules exclude it |
| `inactive` | retired/stale on each selected board | may remain | stopped | none |
| project `paused` | unchanged | unchanged | unchanged | project omitted from active registry routing |
| `removed` | retired where selected | managed reference removed | stopped | none; durable history preserved |

An offline seat is not silently retired. The UI displays last-seen age and
offers Start/Doctor for persistent hosts or restart guidance for ACP hosts. An
inactive seat is not silently started. Reactivation is a fresh plan that proves
the same principal/identity and re-verifies every target board.

## Safe seat removal

Removal is a plan with this order:

1. Resolve the stable principal plus agent projection on every selected board.
   Duplicate names require explicit principal selection.
2. Refuse while any projection holds an active work or review lease. Show ticket
   IDs and require normal submit/unclaim/handoff; removal never steals a lease.
3. Stop new readiness/dispatch advertisement and revoke outstanding offers.
4. Stop the persistent process if managed; an ACP host receives manual close or
   restart guidance.
5. Retire the agent on each board and read back both membership and agent
   projections. A per-board failure leaves the operation `partial`, not removed.
6. Remove only lifecycle-owned managed config blocks and the inventory row,
   using backups and digest guards.
7. Optionally revoke that principal's credential when a targeted mechanism
   exists. Door rotation is a separate, broad-impact action because it affects
   every seat on that role principal.

The confirmation explicitly says what remains: repositories, worktrees, fleet
clones, seat directories, token/CA files, provider secrets, keys/JWKS, logs,
tickets, journal, and backups. A later cleanup tool may inventory these paths,
but this lifecycle never deletes them.

## Safe project pause and removal

Schema v1 already supports `active` and `paused`; pause is the reversible first
step. The plan CAS-writes `status=paused`, verifies registry routing no longer
selects it, then reports live leases, queued work, seats, and door credentials.
Pause does not retire seats or stop processes serving other projects.

Project removal requires the project already be paused, no active lease or
review, no pending offer, complete board/registry snapshots, and typed
confirmation. It CAS-removes only the registry entry. It does **not** delete the
Central board, durable board state, operator folder, fleet clone, Git remote,
keys, JWKS, credentials, or seats. Retiring project-only seats and rotating
doors are separate visible plans. A registry re-add with the prior receipt is
the rollback path.

## Idempotency, recovery, and audit

- Plan IDs are random, single-use, expire after ten minutes, and bind the actor,
  targets, generations, registry SHA, file digests, and requested operations.
- Apply journals `operation_id`, idempotency key, timestamps, redacted result,
  verification state, and backup paths. No token, door, API key, authorization
  header, private key, or private path component enters the journal.
- A replay verifies state and returns the original receipt; it does not repeat a
  one-time secret or irreversible operation.
- CAS/generation conflicts invalidate remaining operations and require a fresh
  plan. No automatic merge changes authority, boards, roles, or credentials.
- Local rollback uses timestamped backups only for unchanged lifecycle-owned
  files. Remote rollback is a new authorized plan.
- A crash-safe job can resume from receipts. Unknown outcomes are checked by
  product reads before retry.

## Proposed API shape

The route names are illustrative; they should live beside the existing guarded
config routes and reuse their loopback/same-origin response headers.

| Method and route | Effect |
| --- | --- |
| `POST /api/lifecycle/discover` | Read-only project/seat observations; no secret values |
| `POST /api/lifecycle/plan` | Create immutable redacted plan |
| `GET /api/lifecycle/plan/{id}` | Read the caller-bound plan until expiry |
| `POST /api/lifecycle/apply` | Consume plan after digest confirmation |
| `GET /api/lifecycle/jobs/{id}` | Read bounded receipts and verification |
| `POST /api/lifecycle/verify` | Run product-state verification/Doctor |
| `POST /api/lifecycle/copy-config` | Return secret-free generated config/prompt with `no-store` |

Door copy/rotate remains on the existing endpoints and must not be embedded in
a normal JSON plan response. Existing `/api/config/plan`, `/api/config/apply`,
registry CAS, Add project, and worker actions become internal operations; they
remain compatible during migration.

## Migration and rollback

1. Add read-only discovery and a combined plan renderer. Keep all current
   buttons; compare results against existing inventory and registry views.
2. Add copy-before-apply and plan digests/expiry to seat configuration. Existing
   inventory schema remains readable; missing lifecycle fields are computed.
3. Route new project creation through combined plans while retaining Add project
   as a compatibility wrapper with the same step receipts.
4. Add pause, seat retirement, and managed-config removal after dry-run tests.
5. Add project registry removal last. Never couple it to filesystem cleanup.

Rollback disables new lifecycle mutation routes and restores backed-up local
config. Existing Central memberships, registry schema, Doors, host adapters,
inventory, and worker controls continue to function. Any remote change already
applied is reported and reconciled with an explicit new plan, never hidden by UI
rollback.

## Acceptance matrix

### Creation and update

1. Register a non-Git folder: no Git command, clone, or remote mutation occurs.
2. Inspect an existing clean Git checkout: plan is read-only until confirm.
3. Dirty or ambiguous Git target: automation blocks and reports the exact safe
   inspection action; existing content is unchanged.
4. Re-run an identical project plan: every operation verifies
   `already_applied`; no new door is minted.
5. Registry SHA changes after plan: apply refuses before mutation.
6. Seat plan preview includes exact managed diffs and no credential contents.
7. Operator copies config and prompt before apply; state remains unchanged.
8. ACP apply writes only the selected adapter, backup, and inventory; restart
   guidance is accurate.
9. Persistent apply does not start until Doctor and explicit start confirmation.
10. Model/provider/tier/skills and role capability constraints round-trip from
    product-produced inventory and Central observations.

### Membership and multi-board behavior

11. Worker, reviewer, coordinator, and orchestrator use their exact role and
    capability constraints.
12. A coordinator spanning three boards verifies the same principal plus three
    explicit memberships; one missing membership yields `degraded`, not ready.
13. `boards=registry` observes a newly activated WORK project without changing
    HOBBY/PERSONAL scope; a dedicated home-board seat does not widen.
14. An explicit board list rejects malformed, duplicate, unauthorized, paused,
    or unknown targets before apply.
15. Principal/agent mismatch blocks even when display name matches.
16. Push verification establishes a live subscription from an authoritative
    cursor; it never uses cursor zero or polling as proof.

### Secrets and authority

17. Browser, HTTP logs, plan, receipt, audit journal, errors, and support export
    contain no token, door, API key, auth header, or private key.
18. Non-loopback, cross-origin, missing nonce, wrong content type, member-only,
    and stale-admin requests all fail before filesystem, registry, JWKS, or
    Central mutation.
19. Door copy is one-time/no-store; list endpoints cannot recover its value.
20. Coordinator setup refuses worker/reviewer door credentials and requires an
    admin-provisioned scoped token-file reference.
21. Lost apply response plus retry cannot rotate a door, issue another door, or
    start a second process.

### Offline, removal, and recovery

22. Stopping an ACP or persistent host shows offline while membership remains
    active; no automatic retirement occurs.
23. Seat removal with an active work or review lease refuses without revoking
    the lease; normal handoff/unclaim remains required.
24. Multi-board retirement verifies every membership and agent projection;
    partial failure is visible and retry only targets unresolved operations.
25. Seat removal preserves all folders, repositories, worktrees, credentials,
    keys, logs, tickets, journal, and backups.
26. Door rotation remains separately confirmed and warns that all seats on the
    former role key must rejoin.
27. Project pause removes active routing while preserving board and filesystem.
28. Project removal refuses until paused and quiescent, then removes only the
    CAS-protected registry entry.
29. File digest change after apply prevents automatic backup restoration.
30. Crash after each operation resumes from real product state and receipts,
    with no expected-value fabrication.

### Compatibility and negative cases

31. Existing Add project, Doors, seat config/import/Doctor, registry view, and
    worker test/start/stop retain their current contracts during rollout.
32. Existing schema-v1 registry and `seats.json` load without migration writes.
33. Plan expiry, reuse by another actor, altered board generation, incomplete
    snapshot, duplicate seat name, missing credential file, unsafe permissions,
    and malformed provider/model values produce bounded errors and no effects.
34. Rollback never deletes an existing path and never converts a remote mutation
    into an unreported success.

## Source map

- Project schema and routing: `packages/client/src/pursers_client/project_registry.py:124-184,250-310`.
- Registry fetch/CAS: `tools/fleet-dashboard/fleet_dashboard.py:4569-4652`.
- Seat/agent retirement UI and calls: `tools/fleet-dashboard/fleet_dashboard.py:5189-5207,7915-7929`.
- Doors and Add project: `tools/fleet-dashboard/fleet_dashboard.py:5835-6180`.
- Fleet clone safeguards: `tools/fleet-dashboard/fleet_dashboard.py:7321-7424`.
- Seat plan/apply/prompt: `tools/fleet-dashboard/fleet_dashboard.py:7040-7101`.
- Host adapters and atomic inventory: `tools/fleet-dashboard/seat_config.py:1111-1655,1897-1945`.
- Doctor's real Central identity/subscription probe: `tools/fleet-dashboard/seat_config.py:2160-2235`.
- Central retirement lease/offer/history behavior: `packages/central/src/pursers_central/central.py:7944-8010`.
- Registry-wide retirement verification: `tools/wait-bridge/README.md:442-459`.
