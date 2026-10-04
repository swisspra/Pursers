# Fleet Dashboard configuration gaps and delivery plan

This plan converts the field-level inventory in [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) into bounded implementation work. It is not authorization to perform live changes. All mutation acceptance uses isolated fixtures, retains compare-and-swap or immutable plan confirmation, and reads the product state back after apply.

## Completion rule

A configuration is covered only when the dashboard provides this full flow:

1. **Read** the effective value, inheritance/provenance, capability availability and authoritative revision.
2. **Edit** with a typed control; paths, identifiers, enums, booleans, lists and secrets are not interchangeable text blobs.
3. **Validate** locally for usability and server-side for authority. Server validation wins.
4. **Preview** exact effects, prerequisites, restart/apply behavior, and preserved state.
5. **Confirm/apply** through the bounded domain API. High-risk operations require deliberate confirmation; a link or button alone is not support.
6. **Read back** from the product-owned state and report partial, stale, unavailable or revision-conflict states honestly.

Generic JSON editors, arbitrary shell execution, copying secrets through the browser, optimistic success without read-back, and links to external documentation do not satisfy this rule.

## Prioritized gaps

| Priority | Domain / missing surface | Current state | Required typed flow | Backend work / owner |
| --- | --- | --- | --- | --- |
| P0 | Delivery action contract | Projects reads `/api/projects/delivery` and sends `action=delivery` through lifecycle plan/apply; the shared endpoint obscures this distinct contract | Load resolved policy → typed scope/preset/branches/gates → server preview → explicit activation → registry and branch read-back | Add focused action/contract tests in `TK-09b833b7aa9cca640ac6`; UI stays with `TK-da4fa65758fe7e6e8f1e` |
| P0 | Complete Settings navigation | Existing `#/settings`, `#/config`, `#/seats`, workers and project editors are scattered | Settings index with Autopilot, Butler, Projects & Doors, Seats & Dispatch, Delivery, Diagnostics, Resources & Skills, Release; retain old deep links | `TK-da4fa65758fe7e6e8f1e`; route styling must wait for foundation `TK-97b702c2b8d66dcac7fb` |
| P0 | Inbox typed source model | `#/approvals` combines actions visually but no canonical `#/inbox`; sources have different permissions | List/detail on mobile; source identity; only Human, Intake and Butler-specific actions; independent review remains unavailable to operator | `TK-925e8c4b9657ebc5f5e5`; retain `#/approvals` alias |
| P0 | Board policy fields | Dispatch editor exists; review, staleness, retention/archive and journal retention are read-only or absent | Board selector → typed policy groups → impact preview → admin writer → board status/read-back | Bounded wrappers in `TK-09b833b7aa9cca640ac6`; UI in Settings ticket |
| P0 | Butler policy layers | Provider editor and kill switch exist; global/project/board policy, scopes, windows, ceilings and auto-demote have no field UI | Inheritance/provenance read → typed safe-scope editor → provider reference validation → preview effective policy → CAS save → read-back | Existing validator can back most writes; add explicit effective-policy preview/read-back in `TK-09b833b7aa9cca640ac6`; UI in Settings ticket |
| P0 | Sonar/UnifiedMCP configuration | No complete baseline schema/handler/editor for sources, grouping, dedup, observations, zero-issue gate or writeback | Connector/source form → credential reference selection → connectivity/permission validation → grouping/dedup preview → zero-issue gate → explicit writeback enable → observations read-back | Missing backend: `TK-09b833b7aa9cca640ac6`; UI must not precede typed API |
| P0 | Project onboarding completeness | Lifecycle plan/apply exists but legacy add and separate clone/Doors/dispatch steps create ambiguous success | Name/board/source/branch/tier → read source → plan registry/board/clone/Doors/default policies → confirm → step receipts → registry, board and Door read-back | Backend orchestration verification in managed-config ticket; guided UI in Settings ticket |
| P0 | Secret-safe prerequisite states | Provider, Door and seat flows each handle secrets differently | Explicit “not configured/configured/invalid/unavailable”; write-only input; private-file target; one-time reveal; no secret echo in errors/history | Cross-cutting tests in backend and Settings tickets |
| P1 | Delivery advanced/runtime distinction | Parser accepts schedules, custom gates, multiple reviewers and repair policy that resident runtime may block | Show inherited/effective value and runtime blocker separately; activation disabled until prerequisites pass; never label saved draft active | Existing readiness model; UI in Settings; backend contract tests in managed-config ticket |
| P1 | Seat/host capability completeness | Most `DesiredSeat` fields are editable, but advanced paths, connector IDs, registry scope, host mode and restart semantics are not coherently grouped | Simple identity/role/model page plus Advanced host/path/scope page → plan/diff → apply → Doctor → explicit restart instruction → readiness read-back | Settings ticket; no arbitrary host-file editing |
| P1 | ACP runner selection | PR 70 adds catalog/preset/install but is outside baseline | Until landed: “pending/unavailable”; after integration: catalog read → compatibility check → preset/session options → install plan → Doctor/read-back | Do not copy PR 70 into this ticket. Reconcile after merge in integration ticket `TK-30865a02f25342f46209` |
| P1 | Doors/membership lifecycle | Doors issue/rotate/revoke is guarded; invite/membership/role/scope flows lack a complete guided surface | Board/role/principal selection → current membership/scopes → plan → confirm → one-time credential delivery if needed → Central read-back | New bounded membership/role API under managed-config ticket; UI in Settings |
| P1 | Agent capabilities/readiness | Team shows identity and readiness; fixed-seat capabilities are host-config fields while transient readiness is Central-owned | Clearly separate “seat template” from “live identity”; template plan/apply/restart versus current readiness read-only; no cross-principal impersonation | Settings/Team UI; backend only if admin-managed readiness is explicitly authorized |
| P1 | Resources, prompts and skills | Seat skill suggestions exist; no discoverable catalog or onboarding state | Catalog/read permission → select allowed resource/prompt/skill → preview host/seat change → plan/apply → reconnect → capability read-back | Determine mutation authority in managed-config ticket; UI in Settings |
| P1 | Diagnostics/import/upgrade state | Jobs exist but results can be displaced by refresh and prerequisites are scattered | Persistent job card with exact action, status, bounded logs, retry/next action and read-back; result survives route refresh | Settings ticket, with focused job-state contract tests |
| P1 | Release and rollback guidance | Exact-plan operations exist; rollback is operation-specific and discoverability is weak | Read readiness → select allowlisted action → immutable command/effect preview → confirmation → streaming job → terminal receipt → supported rollback guidance/read-back | Settings ticket; no generic shell field |
| P2 | Personal profile/app lifecycle | CLI supports setup, activate, doctor, rotate, restart, rollback and uninstall; dashboard has no scoped model | First decide whether Personal belongs in Fleet dashboard. If authorized, use a separate local-only wizard with profile authority and explicit activation | Specific extra package required; do not silently add to fleet Settings |
| P2 | Developer/debug flags | Wait transport timing, connection caps, legacy tools, bridge state paths and raw Central storage switches are environment/process internals | Read-only diagnostic exposure only when actionable; otherwise exclude with rationale and documentation | No operator editor unless a product schema is added |

## Field-level backend gaps

### Board configuration

| Field(s) | Existing authority | Gap | Required API behavior | Failure behavior |
| --- | --- | --- | --- | --- |
| `review_policy` | `BoardClient.board_review_policy_set` | Project setup forces `strict`; no general dashboard writer | `GET` current policy and revision; `POST` allowlisted policy with admin check and read-back | Reject weakening/unknown values; never substitute operator review |
| `stale_after_days` | `board_stale_after_set` | Status only | Typed integer preview explaining identity effects; admin save; board-status read-back | Conflict/permission error leaves old value visible |
| `archive_after_days`, `inline_history_limit`, `invite_prune_after_days` | Central board config | No bounded client/dashboard setter | Add explicit setter(s) with documented ranges and impact; no generic config object | Block unsafe zero/oversize values; preview archive consequences |
| `journal_retention_days`, `journal_row_cap` | Central journal-retention setter | No dashboard route | Typed retention editor, estimated policy effect, admin save and status read-back | Report compaction/unavailability separately from save failure |
| `ticket_progress_v1`, `response_view`, intake rate limit | Central config/process contract | Not an ordinary operator setting today | Keep advanced read-only until migration/compatibility semantics are approved | Do not expose feature toggles that strand clients |

### Butler

| Field(s) | Existing authority | Gap | Required flow |
| --- | --- | --- | --- |
| `answering_mode` | Provider request validator, Board Butler schema and Settings editor | Field is present, but active-mode prerequisites and separate authorization need stronger explanation/read-back | Keep explicit `off/assist/autonomous`; show that provider configuration does not itself create active authorization |
| `global`, `projects.*`, `boards.*` | `coordinator_config.board_butler` CAS | No layer editor or effective preview | Scope selector, inherited-value indicators, reset-to-inherit, effective diff, CAS save/read-back |
| `answer_scope`, `required_evidence_kinds` | Strict validator | No UI | Checkbox/matrix with never-auto classes disabled and explained; server enforces |
| `ceilings`, `hold_before_post_s`, `active_windows`, `auto_demote` | Strict validator | No UI | Numeric typed controls, IANA timezone/day/time editor, validation summary, active-mode prerequisite preview |
| `classification`, `drafting` provider refs | Reference-only schema | No UI; provider references can be confused with secrets | Select managed provider/key references; never accept or display raw key in policy layer |
| Active authorization | Separate Board Butler authorization artifact | Dashboard ordinary save cannot authorize | Show state and prerequisite checklist; provide a guided deliberate authorization only if a bounded backend is approved; otherwise link to operator action and remain unavailable |

### Projects, connectors and delivery

| Field(s) | Gap | Required flow |
| --- | --- | --- |
| Multi-Central project placement | Current pages select Central, but onboarding consequences are not summarized | Choose Central explicitly; show registry board, target board and isolation domain; confirmation binds exact Central and board |
| Repository/source/branch mapping | Lifecycle and delivery planners are separate | One staged wizard may call both, but each plan retains its own digest, expiry and read-back; no combined optimistic success |
| Onboarding tier | Seat tier exists, project lifecycle does not make the intended tier/capability dependency visible | Select/confirm required tier and surface unassignable consequences before completing setup |
| Sonar/UnifiedMCP connector/source/group/dedup/observation | Missing typed product schema and backend | Define allowlisted connector types, source IDs, immutable secret references, grouping/dedup rules and observation records before UI work |
| Zero-issue gate/writeback | Missing backend contract | Preview evaluated issues from product-produced observations; fail closed on incomplete/stale observation; writeback off by default and separately authorized |
| Delivery inheritance/reset | Source supports global/group/repository layers | Surface provenance per field and `reset_to_inherit`; never flatten inherited policy into accidental repository overrides |
| Batch/collection gates | Parser supports pause/release settings; runtime supports only bounded combinations | Show configuration draft versus resident readiness; activation requires zero blockers and exact read-back |

### Seats, models, runners and host capacity

| Field(s) | Gap | Required flow |
| --- | --- | --- |
| Fixed role/capabilities | Editor exists but live identity can be mistaken for template | Present template source path, reconnect requirement and current advertised values side by side |
| Host/global/board caps | Autonomous Butler has host/board concurrency and per-role capacities; managed seat count has no unified ceiling view | Read immutable envelope and host runtime ceiling; enforce min ≤ target ≤ max and summed targets; show headroom before save |
| Drain/start/stop/backoff | Runtime start/stop exists; autonomous cooldowns exist; explicit seat drain lifecycle is not a complete baseline editor | Add typed drain state only after backend guarantees lease/offer behavior; stop must not masquerade as drain |
| Model/provider/profile | Managed seat and persistent worker editors exist separately | Clearly label board-seat model provenance versus local provider runtime; preserve write-only key and restart semantics |
| IDE/ACP | `host_mode` is main; full runner catalog is pending PR 70 | Main shows only supported host mode. Pending controls remain unavailable until exact source is integrated and reviewed |

## Read/save/validate/preview/apply/read-back acceptance patterns

### Dynamic Central state

Use for coordinator thresholds, intake, dispatch, display names and policy fields designed for live updates.

- Read returns an authoritative revision or digest and the effective value.
- Save requires the same revision/digest; stale state returns `409` and preserves form edits.
- Server validates complete allowlisted fields and permissions.
- Successful response is followed by a fresh read; the UI says “saved” only when the value matches.
- No process restart is implied. If a consumer refresh interval delays effect, show that delay explicitly.

### Host-file and connector configuration

Use for managed seats, bridge installs, runtime providers and future ACP runners.

- Read reports source file/config identity without exposing private path values unnecessarily.
- Preview returns a bounded diff, digest, expiry, backup plan, restart requirement and preserved sections.
- Apply consumes the exact plan once. A changed source file invalidates it.
- Doctor verifies files, installed bridge/runtime and authenticated capabilities.
- Read-back separates “file updated”, “host restarted” and “Central observed the new identity”.

### Privileged Central or repository operations

Use for projects, Doors, membership, delivery activation and release actions.

- Read establishes exact Central/board/project, current admin permission, active work and remote/source observation.
- Preview has a short expiry and lists each durable write.
- Confirmation text names the target and cannot be reused for a different plan.
- Apply is idempotent where possible and returns completed/failed step receipts.
- Read-back uses registry/Central/remote state; errors never expose credentials or raw command output beyond the bounded allowlist.
- Restart, publish, final merge and active Butler authorization remain distinct deliberate actions.

## Work packages, dependencies and file ownership

### Package A — managed configuration APIs (`TK-09b833b7aa9cca640ac6`)

Owns backend/schema/tests only:

- `tools/fleet-dashboard/fleet_dashboard.py`
- new focused dashboard configuration modules
- `packages/client/src/pursers_client/` only where a missing bounded Central method is required
- focused `tools/fleet-dashboard/tests/` and client contract tests

Acceptance:

- Every new request rejects unknown fields and unsafe generic payloads.
- Admin/role checks happen before durable writes.
- CAS/plan digest, expiry and read-back are tested with product-produced fixture state.
- Secrets are write-only or server-side references; leak scan passes.
- Delivery `action=delivery` through lifecycle plan/apply has request/response contract tests.
- Board policy, Butler effective preview and connector APIs return explicit dynamic/restart-required semantics.

### Package B — Settings and onboarding (`TK-da4fa65758fe7e6e8f1e`)

Depends on Package A contracts and the shell from `TK-97b702c2b8d66dcac7fb`. Owns:

- `tools/fleet-dashboard/ui/views/settings.js`
- `tools/fleet-dashboard/ui/views/settings.css`
- route-local Settings tests and guided onboarding browser fixtures

It must not take ownership of shared shell/CSS/context until the foundation checkpoint is integrated.

Acceptance:

- Every main-supported operator field in the capability matrix is discoverable from Settings.
- Simple/advanced grouping retains all fields; exclusions have visible rationale.
- Forms preserve unsaved input, focus and server error state across refresh.
- Each save path validates, previews when required, confirms, applies and reads back.
- Dynamic versus restart/reconnect-required is visible before submit.
- Import/export/reset is offered only per typed domain; no arbitrary config JSON download/upload.
- Pending ACP controls are labeled pending/unavailable until their exact source lands.

### Package C — routes and typed Inbox (`TK-925e8c4b9657ebc5f5e5`)

Depends on the foundation shell. Owns route-local Work, Projects, Team, Activity and Inbox view files; it does not own Settings backend.

Acceptance:

- `#/approvals` remains compatible and `#/inbox` has a typed source model.
- Human request, intake, Butler draft and review history never share a generic mutation.
- Mobile list/detail navigation has Back behavior and retains selection.
- Ticket states distinguish offered, working, submitted/review, rework, needs-human and terminal results.
- Delivery states distinguish PR created, integration merged and final team merge.

### Package D — integration (`TK-30865a02f25342f46209`)

Runs after the foundation and Packages A–C. Owns reconciliation and acceptance evidence, not opportunistic feature implementation.

Acceptance:

- Rebase/reconcile exact approved checkpoints and report both working and approved bases.
- Re-run endpoint/field enumeration and fail if a supported field has no matrix row or discoverable surface.
- Exercise isolated product-generated responses for each data-consuming selector and save/read-back path.
- Run route aliases, mobile/desktop accessibility, secret redaction, CAS conflict, stale-plan, permission-denied and restart-state acceptance.
- Reconcile PR 70 only if it has landed; otherwise ACP runner selection remains pending and excluded from shipped claims.
- Full CI and rollout are operator-owned; dashboard rollout uses an immutable revision and rollback pointer.

### Additional bounded package — connector product model

If Package A confirms that Sonar/UnifiedMCP has no authoritative schema, create a separate ticket rather than inventing fields in the dashboard. It owns the connector/source/group/dedup/observation/writeback product model and tests. Settings consumes that contract only after review.

### Additional bounded package — Personal lifecycle decision

Personal setup/activation is a distinct local product authority. A coordinator decision must choose one of:

- exclude it from Fleet Settings and keep a visible scope note; or
- create a separate local-only wizard backed by the existing Personal profile library.

It must not be folded into Central fleet configuration or use a generic shell runner.

## Explicit exclusions and internal/debug settings

These are not silently omitted:

- `PURSERS_LEGACY_TOOLS`: compatibility/debug switch; no operator editor because it changes the exposed tool surface and migration semantics.
- wait-bridge raw state/stat paths, backlog scan intervals, keepalive idle limit and host timeout override: process/debug configuration. Show read-only diagnostics only when they explain a current failure.
- Central `--host`, `--port`, `--data-root`, generation advance and expected-generation digest: service startup/recovery controls. They require deployment tooling, not a browser field.
- arbitrary environment variables, config JSON and shell commands: intentionally unsupported. Add a typed schema and bounded operation before exposing any new setting.
- direct credential copy/rotation: intentionally rejected; only plan/confirm with one-time reveal or configured private-file delivery is valid.
- reviewer approval from an operator session: not authorized; independent review remains a separate principal.

## Remaining uncertainties for implementation preflight

- Delivery policy currently shares lifecycle plan/apply with project add/remove; focused tests must prove each action remains independently validated and cannot consume another action's plan.
- The exact Central ranges and compatibility policy for archive/history fields need a bounded setter contract before UI work.
- Sonar/UnifiedMCP may exist in an external deployment integration not present in this repository baseline. Treat it as unavailable until an authoritative schema and product-produced observation are supplied.
- Running deployment equality was not checked against a live instance; `/api/version` must be captured by the integration worker.
- PR 70 is only a fetched pending source snapshot. Its runner APIs must not be merged, copied or advertised by these docs.
- Personal lifecycle ownership needs a coordinator decision before it can be counted in dashboard completeness.
