# Skill-first installation and AG-UI foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver an install Skill with deterministic bootstrap, a read-only AG-UI observation seam, and a calmer human project journey without waiting for autonomous harness patches.

**Architecture:** The portable install Skill calls a standard-library bootstrap helper using existing packaged Central initialization. Fleet serializes its existing bounded cached projections into finite, read-only AG-UI SSE observations. Human screens retain guarded writes and independent delivery/QA/security truth.

**Tech Stack:** Python 3.12, existing setuptools distributions and portable skill manager, vanilla JavaScript/CSS Fleet UI, existing HTTP server, pytest and Node UI tests.

**Spec:** docs/superpowers/specs/2026-10-27-skill-agui-foundations-design.md

## Global Constraints
- Preserve existing credentials, profiles, instructions, source checkouts, services and active leases. No VM or live runtime mutations.
- No model/provider calls for install detection, observation, refresh or reconnect. Actual package installation occurs only on explicit apply into the planned private root.
- No secrets in model-visible plans, command arguments, stdout/stderr, browser state or public fixtures. Never print subprocess output containing generated credential values.
- Public main is the base. Do not copy pending PR-body/reconciliation WIP or unrelated customer code.
- Keep commands and authority server-bound. Observed state or capability declarations never authorize mutations.
- Preserve framework, logo, routes, themes, keyboard, reduced motion and zoom 200%. No chat or executable generated UI.
- Ready means canonical evidence says ready. Draft publication, QA/security holds and delivery readiness remain separate.
- Use $token-thrift for internal work. No foreground rg/grep. No subagents from workers or reviewers.
- No version bump, merge, push, publish, service start/stop, global skill install or generated-manifest hand edits. Root owns supported artifact regeneration after reviewed changes.

## Review Focus
- Existing/modified/symlinked installation targets must be preserved; Task 1 tests refusal and repeat apply.
- Interrupted setup must produce a truthful resumable or blocked result; Task 1 tests initialization/install failures without credential leakage.
- Partial/stale caches and wrong scope must never appear healthy/empty; Task 2 tests bounded coverage and rejection.
- HTTP 200 or RUN_FINISHED must never imply work completion; Tasks 2 and 3 test observer/run versus domain state.
- Unsafe strings, large text and keyboard/200% zoom must preserve usable controls; Task 3 tests DOM escaping, navigation and layout behavior.

### Task 1: Portable installation Skill and deterministic bootstrap

**Files:**
- Create: integrations/skills/pursers-install/SKILL.md
- Create: integrations/skills/pursers-install/scripts/bootstrap.py
- Modify: integrations/skills/manifest.json
- Modify: delivery-manifest.toml (register bootstrap operator tool only if discovered)
- Modify: docs/guides/portable-skills.md
- Modify: docs/GETTING-STARTED.md
- Modify: CHANGELOG.md
- Test: tools/tests/test_skill_bundle.py
- Create: tools/tests/test_install_skill.py

**Interfaces:**
- Consumes: existing pursers-central init/run CLI and portable bundle manager; exact released pursers==5.2.1.
- Produces: bundled helper CLI `python scripts/bootstrap.py plan|apply|doctor --root PATH` with machine-readable JSON. plan is read-only; apply requires `--apply` and only operates under a plan-owned private root. doctor is read-only and reports central initialization separately from runtime/agent readiness. Include clear capabilities/deferred components.
- Plan fields: schema_version, root, release, steps, blockers, next_action, ready_for_work=false. Never include credential contents.

- [x] Read skill-creator SKILL and current manager/manifest/tests. Preserve scoped host installation semantics.
- [x] Write failing meaningful tests for plan no writes, new apply with controlled subprocess runner, repeat apply, existing foreign root, symlink refusal, failed initialization, secret-free results and skill bundle install/check/remove.
- [x] Implement a self-contained install Skill and helper. Use pinned package in a private venv, canonical Central init only for a fresh owned instance, and atomic receipt for resumability. Do not start services or edit MCP/global host config. Existing installations route to pursers-start rather than reset.
- [x] Test actual temporary bootstrap CLI plan/doctor and manager preview/install/repeat/check/remove. Mock only network/package subprocesses in unit tests; use real filesystem receipts.
- [x] Update guides/changelog with exact available commands and honestly deferred Fleet/harness activation. Commit and write full report with red/green commands/output.

### Task 2: Harness-independent read-only AG-UI observation

**Files:**
- Create: tools/fleet-dashboard/ag_ui.py
- Modify: tools/fleet-dashboard/fleet_dashboard.py
- Create: tools/fleet-dashboard/tests/test_ag_ui.py
- Modify: docs/guides/fleet-dashboard.md
- Modify: CHANGELOG.md

**Interfaces:**
- Consumes: existing Fleet cached bounded project/ticket activity and existing loopback/same-origin guards; no new Central client identity or provider. Add a locked peek-only TimedCache/DashboardCache accessor that never calls get/load/runner or starts refresh; cold cache returns explicit unavailable. Prefer already-warm detail, otherwise an existing fleet summary for that board.
- Produces: GET /api/ag-ui/capabilities, POST /api/ag-ui/observe. A finite response uses application/json capabilities and text/event-stream observation with RUN_STARTED(protocolVersion=1.0), bounded STATE_SNAPSHOT, ACTIVITY_SNAPSHOT where available, RUN_FINISHED. Raw state retains existing authority/coverage/freshness flags and distinct delivery states. Mutation/run/resume capabilities explicitly false/deferred; existing mutation APIs unchanged.
- Observation scope uses the existing query selectors `central` and required `board_id`; RunAgentInput contains threadId, runId, messages=[], tools=[], context=[], state={} (empty or absent optional values). Reject nonempty client messages/tools/state for this read-only endpoint. Unsupported IDs/scope/input must reject before opening SSE. No per-ticket stream, background threads, new dependency, or unbounded history. Validate runId/threadId and read-only scope; client-supplied state/tools never cause actions. Transport failure is not domain failure.

- [x] Write failing tests for lifecycle/schema/order, correct scope, loopback/origin guards, invalid and oversized inputs, unknown versus zero, escaping/secret exclusion, no provider/lease mutations, finite connection release, and a real DashboardCache accessor test proving no loader/client constructor runs on warm or cold observation.
- [x] Add small pure event projection/encoding module and thin HTTP routes using current cache/guard methods. Never emit raw full ticket histories or credential/config dumps.
- [x] Verify with real throwaway HTTP server requests and exact event payloads, including a held Draft fixture. Observation completes without asserting workflow completion.
- [x] Run affected Fleet tests once and document finite observation versus deferred live journal streaming honestly. Commit/report for independent review.

### Task 3: Human project and delivery UX with observation consumer

**Files:**
- Create: tools/fleet-dashboard/ui/ag-ui-client.js
- Modify: tools/fleet-dashboard/ui/index.html (if required for script loading)
- Modify: tools/fleet-dashboard/fleet_dashboard.py (register the new JS asset only)
- Modify: tools/fleet-dashboard/ui/views/projects.js
- Modify: tools/fleet-dashboard/ui/views/projects.css
- Modify: tools/fleet-dashboard/ui/views/work.js (only shared delivery/activity truth if necessary)
- Modify: tools/fleet-dashboard/ui/views/home.js (only the first-project next action if necessary)
- Test: existing Fleet Node/UI harness plus focused new tests within tools/fleet-dashboard/tests
- Modify: docs/guides/fleet-dashboard.md and CHANGELOG.md

**Interfaces:**
- Consumes: Task 2 finite observation endpoint and existing boardHref, guarded project lifecycle/intake, cache refresh and typed activity.
- Produces: optional scoped `PursersAgUi.observe` consumer returning validated view data with AbortSignal; no independent periodic timer. Integrate into existing refresh cadence only. Abort/ignore old-scope responses and preserve legacy read API fallback.

- [x] Write meaningful tests for observer lifecycle/error handling, stale project response rejection and Draft+QA-held/security-pending not-ready labels. Add cases for integration_merged without final QA evidence and unknown/missing coverage. Real browser BASELINE reproduces unavailable Central + zero visible boards incorrectly showing Connect your first project; pin and fix this state, with an actionable connection-recovery label rather than false first-run.
- [x] Clarify Projects: Add work label, explicit current stage/next action, separate publication/QA/security readiness, calm empty-project install guidance. Reuse existing guarded intake entry; do not invent a second submission API or mark a queued request started.
- [x] Move technical identifiers and all-reported-states into discoverable details; retain important project identity and all controls. Label Central as connection rather than coordinator. Keep removal in secondary actions with existing preview/confirmation.
- [x] Add minimal responsive layout and focus/press feedback respecting effective theme tokens and reduced motion; mutations remain click/key-triggered. No new UI framework.
- [ ] Validate DOM behavior and actual rendered desktop/200% zoom/light/dark screens via available browser harness. No fake passing state or model calls. Run affected tests/typecheck, update docs, commit/report.

## Integration and gates
- Root owns supported integration-manifest/component-lock regeneration only if required by deterministic checks, not manual editing. Preserve baseline generator inputs outside this feature.
- Luna3 verifies new end-to-end temporary install/observe/project flow and Apple UX. Luna2 supplies per-task spec AND code-quality verdicts; final broad review after all tasks. Never re-run same-code full suites solely for reassurance.
- Run ci_manifest check and the required complete branch gate once on final combined source, plus existing dashboard typecheck and leak/diff checks. Record baseline/environment failures separately and fix regressions introduced here.
- Preserve pending VM PR-body/reconcile ownership. Recheck native accepted SHA and exact full gate evidence before any later merge/cutover; do not deploy them or this branch early.
