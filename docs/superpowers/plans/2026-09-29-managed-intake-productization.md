# Managed intake and autonomous runtime integration plan

> For implementation: use superpowers:executing-plans task by task in the current session. Native execution only; the operator explicitly disallows subagents and development tickets for this delivery. Checkboxes track work not yet completed; execution rulings below identify scoped substitutions.

**Goal:** Bring the complete managed-intake and runtime behavior into supported Pursers entry points, with a reproducible deployment and no private adapter dependency.

**Architecture:** Keep the current connector/poller/coordinator/executor boundaries. Add declared source observations, a local fleet evidence producer and configuration-driven deployment utilities. Preserve deterministic empty-source gating, semantic decision reuse, authorized mutations and independent writeback.

**Tech Stack:** Python 3.12, existing MCP client/runtime, Git, systemd/launchd adapters, existing private JSON state and ExecutorStore, pytest and the repository CI manifest.

**Spec:** [Integration design and complete inventory](../specs/2026-09-29-managed-intake-productization-design.md).

## Global constraints

- Baseline main 7471a7349556e0f32513ab1a4a5098f2f0141703; candidate branch vm/autorun at f7ad35b2b6670607549203461375c9cb0ec1b3e4 before planning artifacts. Recheck both before implementing or merging.
- Preserve all 12 changed files and predecessor resident-intake dependencies; do not reconstruct the branch from only its last two commits.
- No hardcoded deployment identifiers, private paths, endpoints or credentials in public source or fixtures. Public examples use reserved example domains and /PATH/TO placeholders.
- Active mode remains explicit and authorized. Cap 15 and the four-template roster are deployment settings, not product defaults.
- No paid model call for confirmed empty sources, including after restart; 15-minute backoff for provider failures; ignore source observation timestamps for semantic caching.
- Preserve index source IDs and delivery attempt state; preserve WORK/HOBBY separation and approval requirements.
- Version bumps belong to a release train. Generated manifests/locks are changed only through repository tooling. Publishing remains from the tag under the operator procedure.

## Review focus

1. A native installation must obtain counts; otherwise the zero-issue guard exists but never executes. Owned by Task 1.
2. A source revision update must not bypass the durable PR attempt guard; a moved remote branch must not deliver unreviewed code. Owned by Task 2.
3. A seat with a live lease on another board, or incomplete evidence, must not be stopped as idle. Owned by Task 3.
4. A generated configuration must not silently broaden Git credential scope, source routing or autonomous authorization. Owned by Task 4.
5. Restart and rollback must not replay imported issues or uncertain PR mutations; model-call evidence must remain interpretable. Owned by Tasks 5 and 6.

---

## Task 1: Make source observations native

**Files:** create tools/board-butler/source_observation.py and tests/test_source_observation.py; modify tools/board-butler/board_butler.py, README.md, tests/test_managed_intake.py and tests/test_source_intake.py. The test paths are under tools/board-butler/.

**Interfaces:** SourceObservationPolicy declares read_tool, bounded arguments and count_path, with max_age_s defaulting to 120. SourceCountObservation contains source_id, count (int or None), status (ok/unavailable), observed_at and optional error_class. async observe_source(policy, read_tool, now) returns that observation; read_tool is an injected awaitable adapter to the existing ConnectorRuntime. Public configuration serializes this as an optional source.observation block. Absence means unknown, not zero.

- [x] Add a failing native-resident test using fake MCP responses: two sources with count 0 produce no_open_issues and zero provider calls across 60 refreshes and a fresh backend instance.
- [x] Add table-driven tests for count 1, mixed zero/nonzero, null, bool, negative, malformed path, MCP error, stale observation and omitted observation configuration. Only fresh successful integer zero for every enabled source bypasses the model.
- [x] Run the new source-observation and native-resident tests and record the expected failures.
- [x] Implement declared read-only observation through the existing ConnectorRuntime, including protocol/schema/rate/output bounds. Normalize results into the existing decision context before CentralBackend._source_intake_decide. Remove the need for the deployment subclass's Sonar-specific method.
- [x] Preserve the zero guard, semantic cache, 15-minute backoff and source-empty cache invalidation already implemented. Test changed counts/availability/in-flight work/provider settings versus timestamp-only changes.
- [x] Document an example observation using pageSize=1 and count_path=paging.total. Keep the combined-severity example configurable; do not translate external severity enums implicitly.
- [x] Run pytest tools/board-butler/tests/test_source_observation.py tools/board-butler/tests/test_managed_intake.py tools/board-butler/tests/test_source_intake.py. Expected: all pass without live services or a model key.
- [x] Commit: feat(butler): observe source counts through declared connectors.

**Deliverable:** The normal Butler entry point provides the same count and no-work behavior as the private VM adapter.

## Task 2: Close intake and PR delivery integration gaps

**Files:** modify tools/board-butler/board_butler.py, project_onboarding.py, tests/test_managed_intake.py, tests/test_source_intake.py, tests/test_project_onboarding.py; modify tools/coordinator/coordinator.py and tests/test_coordinator.py only where regressions require a correction.

**Interfaces:** Retain SourceIntakeIndex's existing persisted schema and stable source IDs. Route every _maybe_writeback caller through one durable attempt transition shared with _writeback_pass. Add async preflight_delivery(repository, source_ref, approved_sha, read_ref) -> DeliveryPreflight, where read_ref is an injected approved read-only connector adapter returning the remote full SHA. DeliveryPreflight contains status (verified/needs_operator), observed_sha and reason_code. Bind the adapter through the writeback configuration; validate its tool as read_only before use.

- [x] Add regression tests for source revision changes after an uncertain PR attempt, annotation failure after successful PR creation, crash after recording delivering, and restart. Assert at most one PR mutation and preserved reconciliation state.
- [x] Add a test for the existing-ticket path inside run_cycle, which currently also calls _maybe_writeback. It must obey the same durable attempt guard as the independent pass.
- [x] Add tests for a remote branch whose head differs from the approved SHA and for a repository/project mismatch. Assert zero PR writes and a bounded actionable finding.
- [x] Add tests for source-ID removal with indexed work, unchanged revision deduplication, onboarding retries and board tier creation. Preserve indexes on restart; flag configuration migration rather than silently abandoning indexed tickets.
- [x] Verify source-declared auto does not remove existing domain and authorization checks. Assert non-source intake retains its prior classification/delivery behavior; keep new-board defaults from mutating existing boards.
- [x] Run the named regressions, implement only the required corrections, then run pytest tools/board-butler/tests tools/coordinator/tests tools/ado-connector/test_connector.py.
- [x] Commit: fix(intake): enforce durable approved delivery across all paths.

**Deliverable:** Intake and delivery remain safe through normal refresh, revision updates, failures and restart; empty-source skipping never blocks approval writeback.

## Task 3: Replace the private fleet observation producer

**Files:** create tools/board-butler/fleet_observation.py and tests/test_fleet_observation.py; modify tools/board-butler/board_butler.py, tools/seat-kit/fleet_executor.py, and the existing fleet reconciler/executor tests.

**Interfaces:** Add ExecutorStore.observation_snapshot() as the repository-owned read interface to seat state. LocalFleetObserver.collect(active_boards, snapshots, now) returns the existing fleet/readiness/lease documents through injected service, registry and provider readers. Add --fleet-observation-mode=file|local, default file. Local mode uses explicit executor state/config paths and approved template/provider bindings; it does not infer them from a home directory layout.

- [x] Add tests for absent unit with missing ExecStart, existing untrusted unit, running/starting/draining/stopped seats and persisted generation/transition data.
- [x] Add multi-board tests: one board idle plus another with a live work or review lease cannot produce a globally idle seat. Truncated, missing or expired registry evidence remains unknown.
- [x] Add tests for mismatched principal/name, retired membership, false readiness, unknown provider binding and an unavailable worker model while the drafting model is healthy. Assert no unsafe scale-up/stop action.
- [x] Run the tests expecting missing observer/interface failures; implement the observer using ServiceAdapter and the new store interface, preserving both systemd and launchd support.
- [x] Publish the existing three document formats atomically with mode 0600 and a 120-second freshness bound. Use read-only provider health requests, never chat/completions as a health probe.
- [x] Wire local mode into CentralBackend without a private subclass. Keep explicit file mode behavior unchanged.
- [x] Run pytest tools/board-butler/tests/test_fleet_observation.py tools/board-butler/tests/test_fleet_reconciler.py tools/board-butler/tests/test_autonomous_fleet_e2e.py tools/seat-kit/tests/test_fleet_executor.py.
- [x] Commit: feat(fleet): collect local executor and registry observations.

**Deliverable:** The ordinary resident can keep the configured fleet fed using fresh supported evidence, without importing private deployment scripts or reading raw SQLite columns.

## Task 4: Make configuration and seats reproducible

**Files:** modify tools/board-butler/launch.sh, tools/seat-kit/fleet_executor_provision.py, tools/seat-kit/seat_new.py, and corresponding README/test files. Create tools/board-butler/deployment.py, examples/managed-intake.json, tests/test_deployment.py; create tools/ado-connector/git_credential.py and test_git_credential.py; create tools/seat-kit/event_seat.py and tests/test_event_seat.py. Register new operator tools in delivery-manifest.toml and mandatory test collection as needed.

**Interfaces:** render_managed_intake_deployment(spec, platform) returns named file contents and launch argv without mutating services. credentials_for_request(fields, config_path) returns credential protocol fields only for an approved repository; the CLI accepts Git's get/store/erase operations and writes credentials only for an authorized get. EventSeatRunner consumes approved command argv, seat identity, provider/model/role, registry cursor state and a configurable turn ceiling; model credentials remain in private runtime configuration.

- [x] Add launcher tests proving connector config, source index, onboarding config, active board, observation mode and signed executor flags reach the normal entry point. Invalid or missing active authorization must not silently enable work.
- [x] Add a clean-directory provisioning test with generic paths. Render owner-only secret references/state directories and platform units, preserve unrelated settings, and rerun idempotently without relying on the current VM layout.
- [x] Add Git helper tests: exact allowed HTTPS repo succeeds; another host/org/project/repo, embedded userinfo, unsafe path, missing/symlink/non-private credential file and malformed request return no credential. Never assert or print a real credential.
- [x] Implement scoped Git helper invocation for onboarding and seat operations. Do not replace unrelated global helpers or put the PAT into a remote URL.
- [x] Add event-seat tests: no relevant event means zero model turns; valid event respects role and budget; restart resumes saved authoritative cursors; unknown/new boards bootstrap through supported registry APIs rather than cursor 0 or a magic constant.
- [x] Generate board.sh metadata and runtime provider/model values from the same approved configuration. Remove manual seat-script repair as an installation requirement.
- [x] Add preflight diagnostics for ambiguous repository mapping, inaccessible branches, absent approval-scan tracking refs, cap versus actual template count, authorization expiry and service start limits. Repairs must use explicit configured targets, not guesses.
- [x] Run pytest tools/board-butler/tests/test_deployment.py tools/seat-kit/tests tools/ado-connector and shell syntax checks for rendered launchers.
- [x] Commit: feat(runtime): provision managed intake and scoped repository access.

**Deliverable:** A clean authorized installation can generate the needed service/configuration/seat setup with private credentials kept outside the repository.

## Task 5: Add model-call evidence and native E2E coverage

**Files:** modify tools/board-butler/board_butler.py and tests/test_managed_intake.py; create tools/board-butler/tests/test_managed_intake_runtime_e2e.py. Extend tools/fleet-dashboard/result_visibility.py and its tests only to expose the bounded fields in the existing runtime/result view; no dashboard redesign.

**Interfaces:** An optional intake audit sink receives source observation status, decision reason, model_called, cache reuse, retry_after, model alias, provider response ID when returned, and elapsed time. It excludes credential headers and prompt bodies. Existing callers without a sink remain compatible.

- [x] Add a fake-gateway test returning a response ID and token usage; assert it can be correlated with the intake decision and that cached/empty decisions create no fake provider response IDs.
- [x] Add redaction/bounds tests for errors and provider metadata. Preserve the distinction between timeout with uncertain upstream completion and a request never sent because of backoff.
- [x] Add E2E coverage using real local Central and fake MCP/model/executor boundaries through the ordinary run entry point: observe count -> decide -> intake -> onboard -> source ticket -> simulated independent approval -> preflight -> one PR writeback.
- [x] Add the same test with zero issues across enabled sources and no model credentials, repeated refreshes, and restart. Assert zero model requests while approval writeback remains available.
- [x] Run pytest tools/board-butler/tests tools/coordinator/tests tools/seat-kit/tests tools/fleet-dashboard/tests, then verify all new test files are included in tools/ci_manifest.py collection.
- [x] Commit: test(runtime): verify native intake and model-call accounting.

**Deliverable:** The main product can demonstrate both the workflow and the absence of unnecessary model calls without private instrumentation.

## Task 6: Validate, integrate and cut over

**Files:** regenerate docs/reference/cli.md with tools/generate_reference_docs.py; update the affected READMEs, delivery-manifest.toml and repository-generated integration artifacts only through their generators. Deployment runbooks contain placeholders, not real installation data.

- [ ] Refresh main and inspect conflicts against the exact candidate. Account for all PR #52 changes and every row of the design inventory; keep deployed history intact.
- [ ] Run tools/generate_reference_docs.py and tools/regenerate_integration_manifest.py as required by changed delivered paths. Do not hand-edit component-lock.json or INTEGRATION_FILES.sha256.
- [ ] Run the full strict gate: python tools/ci_manifest.py check; python tools/ci_manifest.py collect --output /tmp/pursers-counts.json; python tools/ci_manifest.py run --jobs 2; python tools/ci_manifest.py verify --input /tmp/pursers-counts.json. Also run python tools/leak_scan.py and git diff --check.
- [ ] Validate a clean Linux installation with the native entry point, and exercise macOS adapter tests. Record which evidence is automated versus live. Do not mark real issue-to-PR acceptance complete while the source has no accessible issues.
- [ ] Prepare one-revision canary cutover and rollback files. Preserve source index, delivery attempts, executor state, authorization references and cursors. Remove private-adapter references only after the native path passes acceptance.
- [ ] Run a canary observation period with confirmed empty sources: zero chat/completion calls, healthy board refresh, no duplicate intake/PR, correct source and authorization diagnostics. Use the existing isolated acceptance path for simulated work.
- [ ] When accessible work and the scoped target exist, record one real approved issue-to-PR flow. Keep this as an explicit rollout gate for production writeback, not a claimed result of unit tests.
- [ ] Integrate reviewable PRs into main only with required checks and the repository's integration requirements satisfied; wait for gh run watch on the main ci workflow after each main push. Do not dismiss or disable required security checks to accommodate a service failure.
- [ ] If a release is requested, use a release-train version change and build/publish from the tested tag; provide the existing operator cutover commands. Do not equate main integration with a completed package release.

**Deliverable:** Main contains a supported reproducible implementation, the canary uses one tested revision, and rollback plus remaining external acceptance requirements are explicit.

## Execution rulings

- Tasks 1–5 are implemented with focused regression evidence. The native E2E uses real in-process Central and the resident backend; onboarding and executor boundaries have separate regression coverage. External MCP/model writes are fake in automated E2E.
- Provider response IDs, timing and decision metadata are recorded; upstream token usage remains in gateway logs. Existing runtime results expose these fields without a separate dashboard redesign.
- Deployment uses existing executor provisioning and separate documented operator preflights, rather than a new combined preflight command.
- Incomplete cross-board holder attribution protects all seats from scale-down. New registry boards wait at most one subscription timeout before bootstrap.
- Implementation and review are performed inline under the operator instruction prohibiting subagents and development tickets.
- First full local gate identified missing test tooling, a macOS zombie-observation portability issue and a moving-HEAD test run. The test environment and fixture were corrected; final frozen-revision results are recorded separately.

## Plan self-review

- Every inventory row has a disposition and owner: core intake Tasks 1–2; private observer Task 3; credentials, mapping, launcher and seats Task 4; billing evidence and E2E Task 5; artifacts, main/release/cutover Task 6. External server defects and credentials remain external dependencies.
- The zero-issue behavior is checked through native configuration, not only a direct helper test.
- Every mutation path is included in delivery tests; process/cache optimizations never replace durable deduplication or approval checks.
- No deployment-specific helper, magic cursor, provider identity or Git global configuration is copied verbatim into the product.
- Integration and live acceptance remain tracked in Task 6. Automated tests do not establish a completed real production issue-to-PR flow; accessible source work and repository credentials are still required.
