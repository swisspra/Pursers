# Fleet Dashboard capability matrix

This inventory is the source contract for the Nocturne dashboard redesign. The
original field audit was generated against `origin/main` at
`e3958382a484afc471d8790c6ad73ba05fe022d2`. The integration candidate is based
on green `origin/main` at `fcce73dca7c5c550b33a1892b17b5e3701c6e4ab`, adds the approved Settings
candidate `5202b4dc1602a4bbf901c6d2e58dcf60ccd9fd2c`, and retains the reviewed ACP
runner source `3b9983629500177ea430b3c94ba104d61c60e6c0` already merged into that base.
This does not assert that a
running dashboard is at that revision: `/api/version` is the read-back source
for the deployed SHA and dirty state.

The status vocabulary is deliberately strict:

- **main**: present in the baseline source.
- **deployed**: only when `/api/version.running_sha` equals the source being assessed.
- **pending**: approved or proposed source outside this integrated candidate.
- **candidate**: present in this integrated branch but not yet merged, released,
  or deployed.
- **CLI/file-only**: supported product configuration without a dashboard writer.
- **read-only**: the dashboard projects state but cannot change it.
- **missing backend**: the requested operator flow lacks a bounded writer or product-owned state.
- **not authorized**: the operation exists but this surface must not perform it.

## Integrated candidate field-to-test map

This table is the authoritative coverage statement for the integrated candidate.
It supersedes baseline gap wording retained later in this document for audit
provenance. A field is listed as covered only when the candidate has a concrete
control, a bounded product API, an authoritative store, and an exact automated
test. Fields intentionally unavailable to the dashboard are listed separately
with their rationale after the table.

All Settings mutation families use the same user flow: read the effective value
and revision, edit typed controls, preview an immutable plan, apply that exact
plan, and read the authoritative state back. A stale revision, denied authority,
partial failure, or mismatched readback remains visible as failure; the UI does
not infer active runtime state from saved configuration.

| User-facing field family | Route and concrete control | Product API | Authority and apply behavior | Exact source/test evidence | Candidate state |
| --- | --- | --- | --- | --- | --- |
| Board review and staleness: `review_policy`, `stale_after_days` | `#/settings` → Review and staleness select/number controls | `GET /api/config/managed`; `POST /api/config/managed/plan`; `POST /api/config/managed/apply` | Central board policy; admin check, two-minute digest, rollback on partial failure, status readback; hot apply | `tools/fleet-dashboard/tests/test_managed_config.py::test_board_policy_plan_apply_reads_back_and_rejects_stale_source`; `::test_board_policy_failed_apply_rolls_back_prior_field` | candidate, complete |
| Central retention: `archive_after_days`, `inline_history_limit`, `invite_prune_after_days`, `journal_retention_days`, `journal_row_cap` | `#/settings` → Retention and bounded history number controls | managed configuration read/plan/apply routes | Central retention revision; setting changes are hot-applied, but archive/prune/compaction maintenance is never run by save | `tools/fleet-dashboard/tests/test_managed_config.py::test_retention_plan_apply_uses_cas_and_does_not_run_maintenance`; `packages/central/tests/test_retention_settings.py` | candidate, complete |
| Memberships: operation, full `principal_id`, role | `#/settings` → Memberships and roles table/form | managed configuration read/plan/apply routes | Central membership; exact principal, admin check, rollback on readback mismatch | `tools/fleet-dashboard/tests/test_managed_config.py::test_membership_plan_apply_uses_exact_principal_and_readback`; `::test_membership_apply_rejects_bad_readback_and_restores_before_state` | candidate, complete |
| Dispatch: `claim_ttl_s`, `offer_ttl_s`, `broadcast_reoffer_s`, `second_opinion`, `fallback_broadcast` | `#/settings` → Dispatch timing controls | `GET/POST /api/dispatch` | Central dispatch setters; allowlisted ranges and immediate readback | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_dispatch_policy_save_validates_and_forwards_exact_contract`; `::test_dispatch_http_endpoints_use_same_origin_json_guard` | main, complete |
| Connector identity and transport: approved IDs, `connector_id`, `enabled`, `transport`, `protocol_revision`, `endpoint_ref`, write-only `secret_ref` | `#/settings` → Projects & sources → Connectors | managed configuration read/plan/apply routes, family `source_connectors` | Validated mode-0600 connector document; private references are redacted and preserved; apply requires restart but never performs it | `tools/fleet-dashboard/tests/test_source_config.py::test_source_store_redacts_previews_and_preserves_private_values`; `tools/board-butler/tests/test_configuration_contract.py::test_connector_contract_normalizes_legacy_and_redacts_without_tool_calls` | candidate, complete |
| Connector tools/resources/safety: tool `name`, `effect`, `replay`, `stable_call_id_field`; resources; risky/denied tools; timeout/input/output/concurrency/rate limits | `#/settings` → each connector → Tools, resources and limits | managed configuration read/plan/apply routes | Same connector document and resident parser; unknown fields and unsafe replay contracts fail closed | `tools/board-butler/tests/test_configuration_contract.py::test_connector_contract_rejects_stale_version_unknown_fields_and_bad_mapping`; `tools/fleet-dashboard/tests/test_source_config.py::test_source_store_rejects_stale_plan` | candidate, complete |
| Source intake: `source_id`, `connector_id`, `enabled`, `list_tool`, `mode`, `content_type`, pagination, grouping limits, observation tool/count/max-age, writeback tool/event | `#/settings` → Projects & sources → source fieldsets and Advanced disclosure | managed configuration read/plan/apply routes, family `source_connectors` | Validated connector/source document; apply only writes the private file, and writeback remains disabled unless explicitly selected | `tools/board-butler/tests/test_source_intake.py::test_source_config_is_generic_and_free_text_is_forced_to_ask`; `::test_poller_routes_two_shapes_dedupes_revisions_and_bounds_unroutable`; `::test_writeback_is_disabled_by_default_and_runs_once_when_enabled` | candidate, complete |
| Source mappings retained by the typed document: `fixed_args`, `items_path`, `field_map`, `routing`, endpoint records | `#/settings` → source form preserves these validated values while exposed fields are edited | managed configuration read/plan/apply routes | Resident parser owns their shape; the dashboard cannot replace them with arbitrary JSON and preserves them across typed edits | `tools/fleet-dashboard/tests/test_source_config.py::test_source_store_redacts_previews_and_preserves_private_values`; `tools/board-butler/tests/test_configuration_contract.py::test_connector_contract_normalizes_legacy_and_redacts_without_tool_calls` | candidate, preservation-covered; not a free-form editor |
| Source onboarding: `domain`, `auto_onboard`, `projects_root`, `per_cycle_cap`, retry limit/backoff, repository URL/integration branch mappings, member roles, delivery activation request | `#/settings` → Source onboarding typed controls | managed configuration read/plan/apply routes, family `source_onboarding` | Validated mode-0600 onboarding document; apply does not clone, create a board, issue credentials, or activate delivery | `tools/board-butler/tests/test_configuration_contract.py::test_onboarding_contract_normalizes_aliases_defaults_and_repository_mapping`; `tools/board-butler/tests/test_project_onboarding.py::test_unknown_project_is_cloned_registered_and_subsequent_intake_routes`; `tools/fleet-dashboard/tests/test_source_config.py::test_source_store_rejects_stale_plan` | candidate, complete |
| Project lifecycle/source: name, board, work directory, repository URL, integration ref, Git mode | `#/projects` and Settings project links → guided plan/apply | `GET /api/config/registry`; `POST /api/lifecycle/plan`; `POST /api/lifecycle/apply` | Project registry, board membership, clone and Doors observations; explicit confirmation and step receipts | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_project_lifecycle_non_git_preview_apply_and_replay`; `::test_add_project_denied_before_any_durable_mutation`; `tools/board-butler/tests/test_project_onboarding.py::test_unknown_project_is_cloned_registered_and_subsequent_intake_routes` | main, complete |
| Delivery inheritance/routing: scope, group, reset-to-inherit, mode, mapped base, integration/snapshot/final branches | `#/settings` → Delivery policy | `GET /api/projects/delivery`; lifecycle delivery plan/apply | Project registry delivery layers; active work and branch observations are rechecked at apply | `tools/fleet-dashboard/tests/test_delivery_settings.py::test_public_delivery_settings_exposes_exact_editable_layers_not_only_effective_policy`; `tools/fleet-dashboard/tests/test_fleet_dashboard_browser.py::test_delivery_shared_layer_editor_preserves_scope_and_does_not_copy_repository_values` | candidate, complete |
| Delivery release/gates: trigger kind/timezone/schedule, PR update, pause, auto-integrate, test commands, reviewers, upstream/independence/conflict policy, activation | `#/settings` → Delivery policy Advanced controls | delivery read plus lifecycle delivery plan/apply | Saved policy and resident-runtime readiness are displayed separately; unsupported combinations remain draft with blockers | `tools/fleet-dashboard/tests/test_delivery_settings.py::test_new_batch_policy_is_saved_as_draft_and_activation_fails_closed`; `::test_ready_batch_activation_writes_revision_bound_record_and_owned_branch`; `tools/board-butler/tests/test_delivery_activation_e2e.py::test_dashboard_activation_roundtrip_drives_one_frozen_customer_pr` | candidate, complete and fail-closed |
| Managed seat identity/scope: host, role, name, Central URL, home board, boards, `registry_board` | `#/settings` → Managed seats → typed Simple/Advanced controls; `#/seats` compatibility | `GET /api/config/seats`; `POST /api/config/plan`; `POST /api/config/apply` | Schema-1 seat inventory and host adapter; immutable diff, restart-required receipt, inventory readback | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_existing_seat_partial_plan_preserves_advanced_fields_and_reads_back`; `::test_existing_seat_plan_rejects_unknown_and_stale_revision_without_writing` | candidate, complete |
| Managed seat credential/connector references: `token_file`, `token_env_var`, `ca_file`, `bridge_command`, `personal_command`, `bridge_name`, `board_connector_name`, config/repository paths | `#/settings` → Managed seats → Advanced host and connector controls | seat inventory/plan/apply routes | Server-side path/identifier validation; token contents never enter UI; restart is deliberate and Doctor supplies observed readback | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_existing_seat_partial_plan_preserves_advanced_fields_and_reads_back`; `tools/fleet-dashboard/tests/test_seat_config.py::test_advanced_connector_fields_fail_closed`; `::test_inventory_and_doctor_redact_token_and_report_push` | candidate, complete |
| Managed seat capabilities/model: tier, skills, can-work, can-review, provider, model, `host_mode` | `#/settings` → Managed seats; Team shows observed values separately | seat inventory/plan/apply routes | Host configuration is desired state; live Central capability/readiness remains observed and may lag until reconnect | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_team_host_mode_round_trips_through_plan_apply_inventory_and_render`; `::test_settings_plan_payload_requires_explicit_host_mode`; `tools/fleet-dashboard/tests/test_seat_config.py::test_role_rejects_incompatible_capability` | candidate, complete |
| Native/ACP runner: catalog entry, preset, provider/model/mode and session options, exact pin/install plan | `#/seats`, linked from Settings → Managed runners | runner catalog plus immutable install plan/apply | Exact pinned cache and server-owned repository bindings; activation defers while a lease is live | `tools/fleet-dashboard/tests/test_runner_setup.py::test_acp_apply_persists_session_options_and_defers_active_lease`; `::test_native_preset_remains_additive_without_acp_runtime`; `::test_runtime_bindings_reject_symlink_escape` | main at `fcce73dca7c5c550b33a1892b17b5e3701c6e4ab`, complete |
| Persistent worker provider: name, provider, base URL, model, write-only API key; test/start/stop/restart | `#/central/{central}/workers`, linked from Settings | `GET/POST /api/workers` and typed worker actions | Local runtime JSON plus OS keychain; active state is process observation, never inferred from save | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_worker_manager_keychain_config_lifecycle_and_adoption`; `::test_worker_provider_test_uses_keychain_without_echoing_secret` | main, complete |
| Board Butler provider: answering mode, endpoint, model, write-only key, key header/prefix, validation/draft paths, protocol, non-secret headers | `#/settings` → Butler, autonomy and budgets | `GET/POST /api/butler`; `POST /api/butler/kill` | Coordinator config CAS plus private 0600 key file; save validates but does not start or authorize Butler | `tools/fleet-dashboard/tests/test_butler_settings.py::test_active_answering_round_trips_and_projects_on_next_cycle`; `::test_http_api_never_returns_key_or_persists_it_to_board_or_repo`; `::test_active_answering_browser_save_and_clean_refresh` | main, complete |
| Board Butler effective policy: global/project/board inheritance, mode, answer scope, evidence kinds, ceilings, hold, windows, auto-demote and provider references | `#/settings` → managed policy projection and Butler controls | managed configuration read plus coordinator CAS provider route | Effective provenance is product-owned; unsafe answer classes remain escalation-only; active prerequisites fail closed | `tools/fleet-dashboard/tests/test_managed_config.py::test_board_butler_effective_policy_resolves_precedence_and_provenance`; `::test_board_butler_safe_defaults_match_authoritative_answer_classes`; `tools/fleet-dashboard/tests/test_butler_settings.py::test_active_answering_rejects_missing_guards_before_provider_or_save` | candidate, complete projection and guarded save |
| Autonomous Butler: mode/runner, role capacities, host/board concurrency, cooldowns, budget period/tokens/cost/calls, connector enables, reconcile/kill/resume commands | `#/settings` → Automation policy | `GET/POST /api/butler/autonomous`; command endpoint | Central revision CAS and immutable envelope; ordinary save always remains shadow; actual state is separately observed | `tools/fleet-dashboard/tests/test_butler_settings.py::test_prepare_autonomous_config_is_shadow_only_cas_and_preserves_envelope`; `::test_autonomous_commands_are_typed_and_bounded`; `tools/fleet-dashboard/tests/test_fleet_dashboard_browser.py::test_autonomous_butler_browser_accessibility_and_conflict` | main, complete |
| Doors: board, role, operation, delivery destination/seat | `#/projects` and Settings links → guided credential plan/confirm | `GET /api/doors`; `POST /api/doors/plan`; `POST /api/doors/confirm` | Board admin; one-time reveal or server-side private-file delivery; direct copy/rotate routes stay rejected | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_guided_credential_private_file_binds_seat_role_board_and_replay`; `::test_doors_endpoints_and_library_calls` | main, complete |
| Coordinator/intake: thresholds, context pressure, integration watch, intake enabled/rate/token reference/categories | `#/config`, linked from Settings → Coordinator | `GET/POST /api/config`; `GET/POST /api/intake` | Coordinator config CAS; token reference only; dynamic readback | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_config_save_writes_only_coordinator_config_with_cas`; `::test_config_hash_page_renders_all_knobs_and_sources`; `::test_intake_approve_and_decline_are_cas_guarded` | main, complete |
| Diagnostics, bridge, maintenance and release operations | `#/settings` → Diagnostics links; `#/seats` → Doctor/import/bridge/release | config job and ops plan/apply routes | Bounded jobs and immutable operational plans; worker does not deploy, release, or restart live seats | `tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_config_ops_endpoint_guards_and_execution`; `::test_ops_confirmation_plan_is_one_time_and_digest_bound`; `::test_seat_config_manager_reviews_imports_and_doctors_discovered_seats` | main, guarded |
| Inbox, work, ticket evidence and human handoff | `#/inbox` (`#/approvals` alias), `#/work`, board/ticket deep links | fleet/board/intake/human/Butler typed routes | Source-specific actions only; independent review cannot be performed by operator UI | `tools/fleet-dashboard/tests/test_nocturne_routes.py::test_typed_inbox_mobile_list_detail_back_and_independent_bounds`; `::test_ticket_detail_projects_bounded_delivery_review_and_question_evidence`; `tools/fleet-dashboard/tests/test_human_dashboard.py::test_resolve_human_request_success_defaults_disposition` | candidate, complete |
| Responsive/refresh contract: light/dark, desktop/mobile/tablet, keyboard, reduced motion, dirty form/focus/filter/scroll preservation | All Nocturne routes | Read endpoints plus route-local state | Clean server updates may render; active edits are preserved; no eager detail fan-out or model request is added | `tools/fleet-dashboard/tests/test_fleet_dashboard_browser.py::test_refresh_cycles_preserve_reader_state_at_desktop_and_mobile`; `tools/fleet-dashboard/tests/test_nocturne_routes.py::test_typed_inbox_mobile_list_detail_back_and_independent_bounds` | candidate, complete |

### Deliberate exclusions

- Raw environment variables, arbitrary JSON, shell commands, wait-bridge state
  paths, Central startup flags, and generation-recovery controls remain internal.
  They do not have a safe typed operator contract.
- `ticket_progress_v1` and `response_view` remain migration/compatibility
  controls, not ordinary dashboard settings.
- Runtime start/stop, deployment, release, version bumps, generated manifest
  refresh, and independent review remain separate authorities. A configuration
  save never claims to perform them.
- Personal profile lifecycle remains in the Personal product CLI. Fleet Settings
  does not merge local Personal authority into Central fleet configuration.
- The dashboard may preserve parser-owned source mapping fields that are not
  individually editable, but it never exposes an arbitrary document editor.

### Visual evidence references

The repository-owned responsive acceptance set is
`docs/design-home/fleet-display-ready-acceptance/after-settings-1440x900.png`
and `docs/design-home/fleet-display-ready-acceptance/after-settings-390x844.png`.
Fresh isolated-fixture Settings captures are recorded at
`artifacts/TK-30865a02f25342f46209/browser/settings-desktop-1440x900.png` and
`artifacts/TK-30865a02f25342f46209/browser/settings-mobile-390x844.png`, with
capture context in `artifacts/TK-30865a02f25342f46209/browser-acceptance.md`.
They are acceptance evidence, not proof of a deployed runtime revision.

## User routes and deep links

| Route | Current view and behavior | Read/write coverage | Redesign target | Status / gap |
| --- | --- | --- | --- | --- |
| `#/home` | Cross-Central summaries, human requests, Butler drafts, attention and team summaries | Read; source-specific actions delegate to guarded APIs | Home | main; truthful partial/unavailable states required |
| `#/projects` | Registry projects, lifecycle plan/apply, delivery policy plan/apply | Read/write | Projects, Settings > Projects | main |
| `#/work` | Cross-board ticket lanes, filters and ticket entry points | Read | Work | main; ticket mutations remain tool-driven |
| `#/team` | Managed seats plus live identities; display-name CAS edit | Read/write for display name and retirement | Team | main |
| `#/approvals`, `#/inbox` | Typed Human, Intake and Butler sources with source-specific actions; `#/approvals` remains an alias | Read/write by typed source | Inbox | candidate; desktop and mobile list/detail behavior covered |
| `#/activity` | Bounded journal-derived activity and relationships | Read | Activity | main; truncation/window must stay visible |
| `#/settings` | Typed source/onboarding, delivery, seats/dispatch, membership, policy/retention, Butler/autonomy and diagnostics editors | Mixed guarded read/write | Settings | candidate; Simple/Advanced controls and readback states covered |
| `#/config` | Coordinator thresholds, intake classification and token-file reference | Read/write with CAS | Settings > Coordinator | main legacy deep link |
| `#/seats` | Seat and managed-runner planning, Doctor/import/bridge, dispatch and release operations | Read/write with plan/apply or job receipts | Settings > Seats, runners & diagnostics | candidate legacy deep link retained and linked from Advanced Settings |
| `#/central/{central}/overhead` | Wait/context pressure diagnostics | Read | Diagnostics | main advanced route |
| `#/central/{central}/config` | Central-specific coordinator config | Read/write with CAS | Settings > Coordinator | main advanced route |
| `#/central/{central}/workers` | Provider-backed persistent worker runtime | Read/write; secret goes to keychain | Settings > Runtimes | main advanced route |
| `#/central/{central}/board/{board}` | Ticket list; optional `?ticket=` focus | Read plus intake | Work / ticket detail | main |
| `#/central/{central}/board/{board}/{tickets,timeline,changes,flow,routes}` | Bounded board views | Read plus intake | Work / Activity | main |
| `#/board/{board}[/{tickets,timeline,changes,flow,routes}]` | Default-Central compatibility alias | Read plus intake | Compatibility | main; retain alias |

The route parser and board tab list are in `tools/fleet-dashboard/ui/assets/app.js:39` and `:50`; the Nocturne navigation registry is in `tools/fleet-dashboard/ui/index.html:22-29` and `tools/fleet-dashboard/ui/view-registry.js`.

## HTTP endpoint inventory

Every endpoint below is registered by `make_handler` in `tools/fleet-dashboard/fleet_dashboard.py:10219-11235`. Mutation endpoints require loopback, same-origin JSON and bounded bodies unless explicitly noted. A UI control is not proof of support; support requires the listed handler and stored authority.

The dynamic handler prefixes are `/api/board/`, `/api/config/jobs/` and `/api/workers/`; the concrete bounded forms are listed below.

### Reads

| Endpoint | Authority / result | Current surface |
| --- | --- | --- |
| `GET /api/version` | Running source revision and dirty state | Shell footer |
| `GET /api/centrals` | Configured Central labels and default | All routes |
| `GET /api/fleet?central=` | Bounded multi-board summary | Home, Work, Projects, Team, Approvals, Activity |
| `GET /api/board/{board}?central=` | Bounded board detail | Board/ticket deep links |
| `GET /api/projects/delivery?central=` | Resolved policy, inheritance, provenance, activation and runtime blockers | Projects |
| `GET /api/config/registry?central=` | Sanitized project registry plus clone/readiness facts | Projects, Seats |
| `GET /api/doors?central=` | Board/role Doors state; loopback and same-origin guarded | Projects, Settings |
| `GET /api/overhead?central=` | Wait-bridge and model-visible context diagnostics | Overhead |
| `GET /api/config?central=` | `coordinator_config` plus CAS digest | Config/Settings |
| `GET /api/butler?central=` | Secret-free provider/Board Butler projection | Settings |
| `GET /api/butler/autonomous?central=&board_id=` | Desired config, actual state, revision and commands | Settings |
| `GET /api/intake?central=&board_id=` | Pending intake asks and CAS digest | Board/Approvals |
| `GET /api/dispatch?central=&board_id=` | Policy, offers, unassignable work and bounded timeline | Seats/dispatch |
| `GET /api/workers?central=` | Persistent runtimes and provider presets | Workers/Team |
| `GET /api/config/seats` | Managed seat inventory | Seats |
| `GET /api/team/seats` | Cross-host managed seat projection | Team/Home |
| `GET /api/config/bridge` | Installed/expected wait-bridge versions | Seats/diagnostics |
| `GET /api/config/release` | Release readiness/status | Seats/operations |
| `GET /api/config/jobs/{32-lower-hex}` | Bounded asynchronous job status/logs | Seats/operations |
| `GET /api/attention` | Local attention dismissal state | Home |

### Writes and action variants

| Endpoint / action | Accepted fields | Stored authority and safety | Current surface |
| --- | --- | --- | --- |
| `POST /api/config` | `config`, `expected_sha256` | Central `coordinator_config`; CAS | Config |
| `POST /api/intake` new | `board_id`, `text` | Central `coordinator_intake`; rate limited | Board |
| `POST /api/intake` decision | `board_id`, `ask_id`, `action`, `expected_sha256`, optional `title` | Intake queue CAS; approve/decline only | Board/Approvals |
| `POST /api/config/plan` | complete `DesiredSeat` payload | Ephemeral immutable plan/digest | Seats |
| `POST /api/config/suggestions` | seat payload | Read-only skill suggestions | Seats |
| `POST /api/config/apply` | `plan_id`, `digest` | Managed host config files; stale/expired plans fail | Seats |
| `POST /api/config/prompt` | seat payload | Returns bounded kickoff prompt; no runtime start | Seats |
| `POST /api/config/doctor` | optional `names` | Asynchronous read-only checks | Seats |
| `POST /api/config/import` | optional `names` | Imports discovered host configs, then Doctor | Seats |
| `POST /api/config/bridge/install` | empty object | Installs pinned bridge through bounded job | Seats |
| `POST /api/config/bridge/upgrade-all` | empty object | Upgrades all managed bridge installs | Seats |
| `POST /api/config/ops/plan` | `action`, optional action fields | 120-second immutable release/ops plan | Operations |
| `POST /api/config/ops` | `plan_id`, `digest` | Asynchronous exact-plan execution | Operations |
| `POST /api/config/registry/clone` | `project` | Fleet-owned clone plus registry CAS | Projects/Seats |
| `POST /api/butler` | provider fields listed below | Validates one bounded request; key stored in private file | Settings |
| `POST /api/butler/kill` | empty object | Sets Board Butler kill switch; no provider deletion | Settings/Home |
| `POST /api/butler/autonomous` | full desired config below | Central revision CAS; dashboard can save shadow only | Settings |
| `POST /api/butler/autonomous/command` | `board_id`, `request_id`, `intent`, `expected_config_revision`, optional command field | Product command record; allowlisted commands | Settings |
| `POST /api/dispatch` | `board_id`, `policy` | Central dispatch and claim TTL setters | Seats |
| `POST /api/agents/retire` | `board_id`, `agent_id` | Central membership lifecycle | Team |
| `POST /api/agents/retire-inert` | `board_id` | Retires only inert identities | Team |
| `POST /api/agents/display-name` | `board_id`, `agent_id`, `display_name`, `expected_revision` | Agent-profile CAS | Team |
| `POST /api/attention` | bounded local attention map | Dashboard state file, not Central | Home |
| `POST /api/human/resolve` | `board_id`, `ticket_id`, `request_id`, `action`, optional `content`, `disposition` | Central typed human request | Approvals |
| `POST /api/butler/mark` | `board_id`, `ticket_id`, `question_id`, `mark` | Central Butler question disposition | Approvals |
| `POST /api/doors/plan` | `operation`, `board`, `role`, `delivery`, optional `seat` | Ephemeral plan; admin check; private destination resolved server-side | Projects/Settings |
| `POST /api/doors/confirm` | `plan_id`, `digest` | Issue/rotate/revoke exact plan | Projects/Settings |
| `POST /api/doors/copy`, `POST /api/doors/rotate` | always rejected | Direct credential paths intentionally disabled | none |
| `POST /api/lifecycle/plan` | lifecycle request with `action=add|remove|delivery` | 10-minute immutable project/delivery plan with source and active-work observation | Projects |
| `POST /api/lifecycle/apply` | `plan_id`, `plan_digest`, `confirmation` | Registry/board/clone/Doors orchestration under lock | Projects |
| `POST /api/projects/add` | `name`, `board_id`, `work_dir`, optional `integration_ref` | Legacy add-project flow; admin required | compatibility only |
| `POST /api/workers` | `name`, `provider`, `base_url`, `model`, `api_key` | Runtime JSON plus OS keychain | Workers |
| `POST /api/workers/{name}/{test,start,stop,restart}` | empty object | Managed runtime action with bounded result | Workers/Team |

## Persisted editable fields

### Coordinator and intake

Source: `validate_coordinator_config`, `tools/fleet-dashboard/fleet_dashboard.py:967-1074`.

| Group | Fields, defaults and validation | Permission / apply behavior | Coverage |
| --- | --- | --- | --- |
| Thresholds | `stale_seconds`, `grace_seconds`, `starved_seconds`, `critical_starved_seconds`, `review_backlog_seconds`: integer `10..86400`; `lease_warning_ratio`: `0.1..1`; `abandoner_drops`: `1..20`; `abandoner_window_days`: `1..365` | Dashboard coordinator identity; Central state CAS; read on next coordinator refresh | main editor |
| Context pressure | `context_watch_tokens_per_poll` default `30000`; `context_compact_tokens_per_poll` default `80000`; `context_trend_compact_ratio` default `1.5`; compact must exceed watch | Same as thresholds; dynamic read | main editor through generic threshold form |
| Integration watch | `integration_watch_since`: null or timezone-aware ISO-8601 | Same CAS; dynamic | main editor |
| Intake | `enabled`, `work_domain_always_ask`: booleans; `rate_per_hour`: `1..20`; `token_path`: null or safe absolute private-file path | Same CAS; token contents never returned; configuration applies without process restart | main editor |
| Intake classification | Every one of `docs`, `tests`, `audit-analysis`, `bug`, `production-code`, `release-ci`, `membership-roles`, `board-registry` must appear exactly once in `auto_categories` or `always_ask_categories` | Same CAS; WORK-domain guard remains authoritative | main editor |

### Projects and delivery

Sources: `project_registry.py`, `project_lifecycle.py`, `delivery_settings.py`, and `pursers_client/delivery_workflow.py:20-195`.

| Group | Fields | Authority / validation | Coverage |
| --- | --- | --- | --- |
| Project identity/source | `name`, `board_id`, `work_dir`, `repository_url`, `fleet_clone_dir`, `integration_ref`, `status`; lifecycle `git_mode` is `none|existing|clone` | Registry state CAS; board admin; URLs reject embedded credentials/query/fragment; paths and Git source are re-observed at apply | main guided lifecycle, legacy add remains |
| Delivery scope/inheritance | `scope=global|group|repository`, `delivery_policy_group`, `reset_to_inherit`; layers are built-in → global → group → repository | Registry CAS; resolved provenance is readable | candidate Settings editor with per-field provenance and reset-to-inherit |
| Delivery route | `mode=per_ticket_pr|batch_pr|branch_only`, `mapped_base`, `integration_branch`, `snapshot_branch_prefix`, `final_pr_target` | Valid short branches; namespace collision checks; final target constraints | candidate Settings editor and lifecycle preview/apply |
| Collection/release | `release_trigger.kind=ready|manual|scheduled`, optional IANA `timezone` and five-field numeric `schedule`; `pr_update=rolling|freeze_on_ready`; `collection_paused`; `auto_integrate`; `final_merge=manual` | Only supported resident combinations can activate; other valid policies remain explicit drafts with blockers | candidate editor with saved-versus-active distinction |
| Delivery gates | `validation.test_commands` (max 20), `required_reviewers` (`1..10`), `independent_review=true`, `require_upstream_policies=true`, `conflict_policy=pause|repair_then_review` | Cannot weaken independent/upstream review; unsupported runtime combinations fail activation | candidate editor with runtime-blocker readback |
| Activation | `activate`, policy revision and deterministic activation ID | Repository-only explicit activation after branch/state revalidation; active work blocks route changes | main guarded plan/apply |

### Managed seats, hosts and runtimes

Source: `DesiredSeat`, `tools/fleet-dashboard/seat_config.py:612-758`.

| Fields | Defaults / validation | Authority, secret and apply behavior | Coverage |
| --- | --- | --- | --- |
| `host`, `role`, `name`, `central_url`, `home_board`, `boards`, `registry_board` | Hosts: `codex`, `codex-cli`, `zed`, `goose`, `claude-code`, `claude-desktop`, `headless`; roles: worker/reviewer/orchestrator/coordinator; safe IDs; `boards=registry|home|comma-list` | Managed local host config; plan/diff/apply; host restart is prompted, not implied | candidate Simple/Advanced editor and merge-preserving plan |
| `token_file`, `token_env_var`, `ca_file`, `bridge_command`, `personal_command`, `bridge_name`, `board_connector_name`, `config_path`, `seat_dir`, `repository` | Safe path/command/identifier checks in planner; examples must use `/PATH/TO/...` | Private paths remain server-side; token content/fingerprint never enters UI | candidate Advanced editor; formerly omitted fields are preserved on partial update |
| `tier_max`, `skills`, `can_review`, `can_work`, `model`, `provider` | Tier `1..3`; safe unique skill IDs; role/capability combinations fail closed; model/provider ≤200 chars | Written to host connector environment and advertised at onboarding; restart/reconnect required | main editor |
| `host_mode` | `host_mode=acp|persistent` | Host configuration; restart required | candidate editor; native and ACP choices remain distinct |
| Persistent runtime | `name`, `provider`, `base_url`, `model`, write-only `api_key`; providers include DeepSeek, Qwen, OpenRouter, Azure, Ollama, custom | Local runtime file plus OS keychain; test/start/stop/restart are explicit | main editor |
| ACP runner catalog/preset/install | runner ID, binary/source, session options, provider/model/mode selection | Bounded catalog refresh, exact pin, immutable plan/apply, cache integrity and lease-aware activation | candidate at `3b9983629500177ea430b3c94ba104d61c60e6c0`; not released or deployed |

The original baseline seat form omitted the five fields below. The integrated
candidate fixes that defect: Settings renders typed Advanced controls, and a
partial update is merged with the revision-bound inventory record before the
plan is created. `GET /api/config/seats` returns the effective values and
revision; `POST /api/config/plan` previews provenance for every preserved field;
`POST /api/config/apply` reads the resulting inventory back without restarting a
host.

| Field | Exact source, stored authority and default/validation | Current view/action and read/write state | Apply/restart, secret handling and target |
| --- | --- | --- | --- |
| `registry_board` | `DesiredSeat.registry_board`; schema-1 inventory record; default `pursers`; `SAFE_NAME` validated | Settings Advanced scope control; omission preserves the revision-bound inventory value | Restart/reconnect required; Doctor verifies membership and the registry anchor; non-secret |
| `token_env_var` | `DesiredSeat.token_env_var`; default `ONBOARD_CENTRAL_TOKEN`; `ENV_NAME` validated | Settings Advanced credential-source control; the variable name is readable but its value is never returned | Restart/reconnect required; Doctor verifies the token source without echoing credentials |
| `personal_command` | `DesiredSeat.personal_command`; default `pursers-personal`; bounded command/path validation | Settings Advanced host control; arbitrary shell text is rejected | Restart required; Doctor reports executable/version observation |
| `bridge_name` | `DesiredSeat.bridge_name`; null derives `pursers-wait-{name}`; safe ID required | Settings Advanced wait-connector control; omission preserves a customization | Restart/reconnect required; Doctor verifies connector and identity |
| `board_connector_name` | `DesiredSeat.board_connector_name`; null derives a role-specific name; safe ID and collision checks apply | Settings Advanced direct-board connector control; both connector names validate together | Restart/reconnect required; Doctor verifies authenticated principal/role |

For all five fields, the candidate reads the authoritative seat record and
revision, renders typed Advanced controls, merges an edited allowlisted field into
the complete record, validates and previews the exact host-file diff, applies an
unexpired digest, and reads inventory back. Restart remains a separate deliberate
operation followed by Doctor observation. The server rejects stale revisions and
unknown fields and never synthesizes defaults over customized stored values.

### Dispatch, board policy and Central retention

| Group | Fields | Authority / apply | Coverage |
| --- | --- | --- | --- |
| Dispatch | `claim_ttl_s` and `offer_ttl_s` `1..86400`; `broadcast_reoffer_s` `60..86400`; `second_opinion`, `fallback_broadcast` booleans | Board admin; Central setters; dynamic | main editor |
| Review | `review_policy` | Board admin; Central `board_review_policy_set` | candidate typed editor with immutable plan, readback and rollback |
| Staleness | `stale_after_days` default `3` | Central `board_stale_after_set` | candidate typed editor with immutable plan and readback |
| Archive/history | `archive_after_days` default `2`; `inline_history_limit` default `50`; `invite_prune_after_days` default `7` | Central retention revision; operational consequences | candidate typed editor; save never runs maintenance |
| Journal | `journal_retention_days` default `7`; `journal_row_cap` default `50000` | Central retention setter; compaction remains separate | candidate typed editor with CAS and readback |
| Response/progress | `response_view=compact`; `ticket_progress_v1=false`; intake rate limit default `10/hour` | Central board config | read-only/internal; progress toggle requires migration decision |

### Butler provider and policy

Sources: `butler_settings.py:893-1234` and the `board_butler` validator.

| Group | Fields | Authority / validation | Coverage |
| --- | --- | --- | --- |
| Provider | `endpoint`, `model`, write-only `api_key`, `extra_headers`, `key_header`, `key_prefix`, `validation_path`, `draft_path`, `draft_protocol`, `answering_mode`, `expected_sha256` | HTTP(S), no URL credentials/query/fragment; non-loopback requires HTTPS; secret header values forbidden in readable fields; model validated with one bounded call; settings CAS | main editor |
| Policy layers | `board_butler.global`, optional `projects.*`, `boards.*` | Stored inside coordinator config with CAS; inheritance is product-owned | candidate effective projection with layer provenance/reset semantics |
| Mode/scope | `mode=shadow|active`, `answering_mode=off|assist|autonomous`, per-class `answer_scope` | `scope_change`, `gate_waiver`, `release`, `membership`, `registry` can never auto-answer | candidate provider/policy controls; server enforces escalation-only classes |
| Evidence/limits | `required_evidence_kinds`, `ceilings.per_hour|per_ticket|per_board`, `hold_before_post_s`, `active_windows` | Bounded IDs/counts, IANA timezones; active answering requires complete guards | candidate guarded save and effective readback |
| Safety | `kill_switch`, `auto_demote.veto_count|failure_count|window_s` | Kill available; active requires explicit authorization outside ordinary save | candidate guarded controls; ordinary save cannot authorize runtime |
| Task providers | `classification` and `drafting`: `model`, `endpoint_ref`, `key_ref`, headers and relative paths/protocol | References only; secrets stay in managed files | candidate effective projection; provider secret stays outside readable policy |

### Autonomous Butler

Source: `prepare_autonomous_butler_config`, `tools/fleet-dashboard/butler_settings.py:405-590`.

| Fields/actions | Validation and authority | Coverage |
| --- | --- | --- |
| `mode=shadow|autonomous`, `runner=direct_api|acp` | Dashboard save always resolves to shadow and removes authorization; autonomous requires separate active authorization | main, correct separation |
| `capacity.{worker,reviewer,acp_worker}.{min,target,max}` | Each `0..100`, ordered, below immutable envelope | main editor |
| `host_concurrency`, `board_concurrency` | `1..256`, envelope/runtime ceilings, targets must fit | main editor |
| `cooldowns.scale_up_s`, `scale_down_s`, `failure_backoff_s` | bounded; failure backoff at least one second | main editor |
| `budget.period`, `max_tokens`, `max_cost_microunits`, `max_external_calls` | period `hour|day|month`; below immutable envelope | main editor |
| Connector `enabled` | IDs fixed by provisioned envelope; references are not returned | main editor |
| Commands | `reconcile_now`, `enable_connector`, `disable_connector`, `kill`, `resume`; revision/idempotency guarded | main handler; UI exposes only the subset relevant to current cards |

### Doors and identities

| Capability | Authority / secret handling | Coverage |
| --- | --- | --- |
| List board/role Doors | Central JWKS and private key directory; public status only | main read |
| Issue, rotate, revoke | Board admin; immutable plan/digest; one-time reveal or server-side private-file delivery | main guided flow |
| Direct copy/rotate | Explicitly rejected | not authorized by design |
| Agent capability/readiness | Central agent methods; advertised by the authenticated identity | status visible; self-service generic editor intentionally absent |
| Display name | Agent-profile revision CAS; admin/self rules | main editor |
| Retire identity/inert identities | Central lifecycle guard and live-lease checks | main action |
| Membership/roles | Central membership tools | candidate exact-principal plan/apply/readback flow; invite issuance remains a separate credential operation |

## Operational feature coverage

| Domain | Source symbol/file | Current capability | Redesign surface | Status / target package |
| --- | --- | --- | --- | --- |
| Ticket lifecycle | `BoardClient.ticket_create/claim/update/progress_update/annotate/submit/review/cancel/list/history_list` in `packages/client/src/pursers_client/client.py:1206-1663` | Dashboard reads all states/evidence; intake and human decisions write; ordinary ticket work remains authenticated-agent tooling | Work, ticket detail, Inbox | main read; route/Inbox package `TK-925e8c4b9657ebc5f5e5` |
| Questions and human requests | `ticket_question_ask/answer`, `ticket_request_human/human_resolve` | Human resolution and typed content are writable; generic conversation reply is not invented | Inbox | main; typed unification package |
| Review independence | Central review principal checks and delivery policy validation | Reviewer actions are not exposed as operator substitutes | Work/detail | main guard; retain |
| Journal/search/pagination | board detail views, activity projection, ticket list/history pagination | Bounded windows, cursors and truncation | Activity/search | main read; no unbounded Home fan-out |
| Attention | `activity_visibility.py`, local attention state | Dismiss/restore presentation only; does not resolve source item | Home/Inbox | main |
| Usage/overhead | `read_overhead_stats`, `/api/overhead` | Source/window-aware diagnostics | Activity/Diagnostics | main read; cost remains unknown unless observed |
| Resources/prompts/skills | Central resources and MCP tools; seat skill suggestions | Resource/prompt state stays tool-authorized; seat skills are edited through the managed-seat plan | Settings > Managed seats | candidate for seat skills; generic resource mutation deliberately unavailable |
| Connector sources (including Sonar/UnifiedMCP-shaped adapters) | Typed connector/source/grouping/observation/writeback contract | Secret-safe read, typed edits, restart-required plan/apply and resident-parser readback | Settings > Projects & sources | candidate via managed configuration contract |
| Doctor/import/bridge | `SeatConfigManager.doctor/import_discovered/install_bridge/upgrade_all` in `tools/fleet-dashboard/fleet_dashboard.py:7769-9327` | Guarded jobs and bounded logs | Settings > Diagnostics | main |
| Release/stage/restart | `tools/fleet-dashboard/release_ops.py`, `POST /api/config/ops/plan` and `POST /api/config/ops` | Exact expiring plan then async job; rollback/status projected | Settings > Release | main, high-risk grouping |
| Rollback | Release plan receipts and release status | Operation-specific, not a generic rollback button | Settings > Release | main partial; never shell shortcut |
| Personal profile/app setup | `packages/personal/src/pursers_personal/cli.py` | CLI supports setup/activate/doctor/rotate/restart/rollback/uninstall | Guided onboarding | CLI/file-only; dashboard scope needs explicit product decision |

## Evidence boundary

The baseline portion of this inventory began as a static source audit. Candidate
claims in the authoritative table above are exercised with isolated
Central/registry/host fixtures and product-produced responses. No production
configuration, Door, repository, seat, provider, runtime, merge, deployment, or
release is mutated by those tests. Generated delivery locks and the full strict
CI gate remain operator-owned.
