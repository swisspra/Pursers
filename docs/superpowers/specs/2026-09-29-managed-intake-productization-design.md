# Managed intake and autonomous runtime integration design

Status: proposed integration design, based on inspected code and the running deployment. This document does not claim that the remaining integration work is implemented.

## Outcome

A supported Pursers installation can observe external work, decide intake without unnecessary model requests, onboard authorized repositories, run workers and reviewers, and open a pull request after approval. The ordinary repository-owned entry point must provide this behavior without a private backend subclass or manually edited generated seat scripts.

The implementation remains native in the current working session. No development tickets or subagents are required for this integration, per the operator's direction. Use $token-thrift for internal work while preserving complete human-facing documentation.

## Inspected baseline

- Target main: 7471a7349556e0f32513ab1a4a5098f2f0141703.
- Implementation branch: vm/autorun at f7ad35b2b6670607549203461375c9cb0ec1b3e4, PR #52.
- Difference from main: 12 files, 2,120 additions and 179 deletions, including the predecessor's resident-intake implementation. Do not cherry-pick only the later fixes and omit that prerequisite.
- Latest focused validation: Butler/coordinator 595 passed locally; Linux 594 passed and one expected platform skip. This is not a full real issue-to-PR acceptance test.
- The deployed Butler uses the latest source, while coordinator and executor use an earlier compatible source snapshot. The cutover must converge these onto one tested revision.
- The deployment currently has four approved seat templates and an operator cap of 15. A cap is an upper bound, not proof that 15 seats exist.
- Real end-to-end acceptance remains pending accessible open issues and the replacement credential. Current source observations confirm zero open issues.

## Complete disposition inventory

| Change or behavior | Current location | Main-repository disposition |
| --- | --- | --- |
| Resident source scheduling, connector startup handling, durable findings | PR #52, board_butler.py and test_source_intake.py | Retain, including predecessor changes |
| LLM chooses pull count and source order; hard seat ceiling | PR #52, SourceIntakePoller and CentralBackend | Retain; no fixed formula replaces the model for nonempty work |
| Fresh idle worker/reviewer counts and queue load | PR #52, source_intake_board_load | Retain; stale, busy, leased, non-dispatchable agents do not count as idle |
| Known zero issues bypass the model | PR #52, CentralBackend | Retain; wire native count collection so the guard actually receives counts |
| Semantic decision cache and 15-minute provider failure backoff | PR #52, IntakeDecisionCache | Retain; observation timestamps alone never invalidate the cache |
| Paged source reads, revision handling, private issue index | PR #52, SourceIntakeIndex | Retain; validate restart, crash, configuration migration and deduplication behavior |
| Exact-operation connector grant and approval-triggered PR writeback | PR #52 | Retain; consolidate every delivery path behind the durable attempt guard |
| Tools-only MCP discovery without resources/list | PR #52, ConnectorRuntime | Retain; preserve existing resource-enabled connectors |
| Auto source tickets, production-code category, push-branch instructions | PR #52, coordinator.py | Retain; regression-test the existing domain/authorization gates and non-source intake |
| New-board default ticket tier | PR #52, project_onboarding.py | Retain; do not change existing boards implicitly |
| Missing systemd unit without ExecStart | PR #52, fleet_executor.py | Retain; an existing or untrusted unit remains protected |
| Sonar paging-total observation before model decisions | Private deployment adapter | Move into a reusable, declared, read-only source-observation contract |
| Service/process, host, registry membership, lease and provider evidence | Private deployment adapter | Move into a supported local observation producer; keep existing file input mode |
| Scoped Git credential helper | Private deployment script and Git configuration | Add a configurable helper and onboarding integration; never copy credentials or deployment paths |
| Event-driven Goose seat launcher and corrected model metadata | Private deployment script and generated board.sh files | Generate from approved configuration; remove hardcoded seat names, models and bootstrap cursor |
| Active Butler launch flags, onboarding file, source index and executor paths | Private service units | Add repository-owned rendering/provisioning and example configuration |
| Host cap command, warm pool, template limits and authorization expiry | Existing product APIs plus private operator configuration | Keep as operator configuration; do not make cap 15 or the four-template roster universal defaults |
| Repository-to-source inventory and ambiguous mapping exclusions | Private inventory/configuration | Product owns validation and optional read-only discovery; operator owns access and final mapping |
| Combined-severity source workaround | Private connector configuration | Ship as a supported configuration example, not a hardcoded universal policy |
| Tracking reference needed by approval scan | Deployment repair | Add preflight diagnostics; do not silently create or rewrite user branches |
| CLI documentation, leak checks and generated artifacts | PR #52 and repository tooling | Regenerate only through repository generators and verify in CI |
| External server severity mismatch and GitHub AI model-service failure | External services | Track separately; do not claim these were repaired by a Pursers merge |

## Decisions and boundaries

### Source observation and model use

Each configured source may declare a count observation using an approved read-only tool, bounded arguments and a response path. A Sonar example requests page size 1 and reads paging.total. No issue body or repository content is needed for this count.

An observation is either a validated nonnegative integer count or unknown with a bounded error class. Missing counts, booleans, negative values, malformed responses, failed requests and stale observations are never interpreted as zero. Unconfigured observation support remains unknown for compatibility.

The decision order is:

1. Run the independent approved-ticket writeback pass.
2. Obtain fresh source observations and current board/capacity facts.
3. If every enabled source is successfully observed as zero, return pull=0, reason=no_open_issues, model_called=false before resolving a provider.
4. Otherwise reuse a successful decision only when its semantic inputs and provider configuration are unchanged.
5. When inputs change, ask the model. Validate the response and clamp it to the operator ceiling.
6. On model failure, pull nothing and defer another attempt for 15 minutes, unless the provider configuration changes.

The current 60-second board refresh is not a 60-second model schedule. Source reads and board observation may continue without LLM calls. Source observation timestamps alone are excluded from the decision fingerprint. The cache is process-local; restarting with confirmed empty sources must still make zero model calls. Observing empty sources invalidates a previously successful nonempty decision without clearing failure backoff.

### Fleet evidence

The existing file-based observation interface remains supported. A new opt-in local mode obtains observations through the existing systemd/launchd adapters and an explicit ExecutorStore snapshot interface. It must not query undocumented SQLite columns from Butler code.

Observe membership, lifecycle, capabilities, readiness and live work/review leases across all selected active boards. A busy seat on another board is not idle or safe to stop. Truncated or failed snapshots are unknown. Publish the existing observation/readiness/lease formats atomically with owner-only permissions and a 120-second freshness bound.

Provider observations must use explicit template/provider bindings. A healthy drafting-model endpoint does not prove that every worker model is available. Missing bindings yield unknown evidence. Provider health reads must not create paid chat/completion requests.

### Intake, repository access and delivery

Repository mappings remain explicit and validated against authorized access. Duplicate names across projects require a unique mapping; names alone cannot authorize a target. Preserve WORK versus HOBBY/PERSONAL separation. The source tier default applies at board creation only.

The supported delivery path remains local Git for checkout, changes, tests and push; MCP handles external source reads and PR creation. A full MCP-only Git workflow is outside this integration.

Git credentials are resolved from private file references and emitted only through Git's credential protocol for an exact approved HTTPS host and repository path. Do not put secrets in URLs, argv, logs, prompts or tickets. Configure the helper for generated operations or approved repository scopes; do not replace unrelated global Git configuration.

Before PR creation, verify independent approval, the intended repository/project, source and target branches, and the approved full SHA. A pushed branch that no longer points to the approved SHA must not be delivered silently. All writeback paths must durably record their attempt before the external mutation. Timeout or uncertain result must not cause an automatic duplicate PR; expose an operator reconciliation state instead.

The current source index keys include source_id. Keep source IDs stable during this cutover and preserve the existing index and attempt states. Renaming or regrouping sources with indexed work requires an explicit migration; startup validation must flag missing indexed source declarations. Severity regrouping is deferred until that migration and the external server contract are verified.

### Runtime and configuration ownership

Goose is selected for CLI-friendly unattended seat execution. Zed remains the
primary GUI/IDE direction, using the existing Pursers MCP/ACP integrations.
Keep orchestration and delivery contracts independent of the chosen host; the
Goose event runner is a CLI implementation, not a universal GUI requirement.
This delivery documents the existing Zed path rather than adding new GUI behavior.

Pursers owns the mechanisms, schemas, launchers, diagnostics and tests. Deployment configuration owns service paths, endpoints, credential references, repositories, models, seat templates, cap values and authorization lifetimes. None of the current deployment's identifiers or secrets belongs in public fixtures or defaults.

Use the existing authorized executor and host-cap command path. Do not add another process manager, bypass authorization expiry, or make active mode the installation default. Existing shadow and file-observation deployments must continue working.

Expose decision reason, model_called, cache reuse, retry time and available provider response/request IDs in bounded audit records. Do not log prompt bodies or credentials by default. This provides correlation for future gateway billing questions without reconstructing historical requests from timestamps.

## Delivery sequence

1. Complete PR #52's native source-observation path and validate its intake/writeback invariants.
2. Add the reusable local fleet observer and remove the private backend subclass dependency.
3. Add reproducible launch/configuration, scoped credentials and event-seat generation.
4. Validate the ordinary entry point on a clean installation and cut over the canary to one exact revision.
5. Integrate the tested branch into main under repository policy, watch full CI after the push, and use the existing release-train/tag process if a release is requested.

Prefer reviewable commits grouped by these boundaries. PR #52 can carry the cohesive intake work; runtime/configuration work may be separate dependent PRs. Do not rewrite already deployed history or merge a partial configuration that claims the private adapter has been retired.

## Acceptance and rollback

Required acceptance: native entry point without private imports; zero issues => zero chat/completion requests across refreshes and restart; changed nonempty demand reaches the model; unchanged demand does not; no duplicate intake after restart; approval writeback works while intake is skipped; cap/expiry/lease constraints hold; scoped Git credentials never cross repository boundaries.

Run the full CI parity gate on the exact integration candidate, including manifest coverage, all suites, generated docs/artifacts and leak checks. Keep Linux execution evidence and macOS adapter tests. Required repository checks remain authoritative; an external AI-scanner service error is not a code pass or grounds to disable scanning.

Separate three evidence levels: automated fake-service E2E, live read-only/provider/executor checks, and a real issue-to-approved-PR acceptance case. The third remains incomplete until accessible work and an explicitly scoped test target exist. Do not create a production test PR merely to make the checklist green.

Before cutover, preserve unit files, configuration, index, executor state, cursors, authorization references and the prior runtime revision. Restore code and service configuration on rollback while preserving the newest durable delivery/index state so an uncertain external mutation cannot be replayed. A release is built and published from its tag, using the existing operator procedure.
