# Fleet Dashboard capability matrix

This inventory is the source contract for the Nocturne dashboard redesign. It was generated against `origin/main` at `e3958382a484afc471d8790c6ad73ba05fe022d2`. It does not assert that a running dashboard is at that revision: `/api/version` is the read-back source for the deployed SHA and dirty state. PR 70 was inspected separately at `ca1be1763b1d8fa8074805492081cec9093df954`; its runner catalog and ACP setup APIs are **pending, not released or deployed**.

The status vocabulary is deliberately strict:

- **main**: present in the baseline source.
- **deployed**: only when `/api/version.running_sha` equals the source being assessed.
- **pending**: approved or proposed source outside this baseline, including PR 70.
- **CLI/file-only**: supported product configuration without a dashboard writer.
- **read-only**: the dashboard projects state but cannot change it.
- **missing backend**: the requested operator flow lacks a bounded writer or product-owned state.
- **not authorized**: the operation exists but this surface must not perform it.

## User routes and deep links

| Route | Current view and behavior | Read/write coverage | Redesign target | Status / gap |
| --- | --- | --- | --- | --- |
| `#/home` | Cross-Central summaries, human requests, Butler drafts, attention and team summaries | Read; source-specific actions delegate to guarded APIs | Home | main; truthful partial/unavailable states required |
| `#/projects` | Registry projects, lifecycle plan/apply, delivery policy plan/apply | Read/write | Projects, Settings > Projects | main |
| `#/work` | Cross-board ticket lanes, filters and ticket entry points | Read | Work | main; ticket mutations remain tool-driven |
| `#/team` | Managed seats plus live identities; display-name CAS edit | Read/write for display name and retirement | Team | main |
| `#/approvals` | Human requests, intake decisions and Butler draft marks | Read/write by typed source | Inbox, with `#/approvals` alias | main; Inbox route itself is missing |
| `#/activity` | Bounded journal-derived activity and relationships | Read | Activity | main; truncation/window must stay visible |
| `#/settings` | Guided links plus Butler/provider and autonomous settings | Mixed | Settings | main but incomplete editor coverage |
| `#/config` | Coordinator thresholds, intake classification and token-file reference | Read/write with CAS | Settings > Coordinator | main legacy deep link |
| `#/seats` | Seat planning, Doctor/import/bridge, dispatch, release operations | Read/write with plan/apply or job receipts | Settings > Seats & diagnostics | main legacy deep link |
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
| Delivery scope/inheritance | `scope=global|group|repository`, `delivery_policy_group`, `reset_to_inherit`; layers are built-in → global → group → repository | Registry CAS; resolved provenance is readable | main planner; Settings discoverability incomplete |
| Delivery route | `mode=per_ticket_pr|batch_pr|branch_only`, `mapped_base`, `integration_branch`, `snapshot_branch_prefix`, `final_pr_target` | Valid short branches; namespace collision checks; final target constraints | main source; endpoint acceptance required |
| Collection/release | `release_trigger.kind=ready|manual|scheduled`, optional IANA `timezone` and five-field numeric `schedule`; `pr_update=rolling|freeze_on_ready`; `collection_paused`; `auto_integrate`; `final_merge=manual` | Only `ready` resident runtime is currently installed; unsupported combinations remain draft with blockers | partial: editor may save more than runtime supports |
| Delivery gates | `validation.test_commands` (max 20), `required_reviewers` (`1..10`), `independent_review=true`, `require_upstream_policies=true`, `conflict_policy=pause|repair_then_review` | Cannot weaken independent/upstream review; resident runtime currently supports no custom per-ticket commands, one reviewer and `pause` | partial; read blocker before activation |
| Activation | `activate`, policy revision and deterministic activation ID | Repository-only explicit activation after branch/state revalidation; active work blocks route changes | main guarded plan/apply |

### Managed seats, hosts and runtimes

Source: `DesiredSeat`, `tools/fleet-dashboard/seat_config.py:612-758`.

| Fields | Defaults / validation | Authority, secret and apply behavior | Coverage |
| --- | --- | --- | --- |
| `host`, `role`, `name`, `central_url`, `home_board`, `registry_board`, `boards` | Hosts: `codex`, `codex-cli`, `zed`, `goose`, `claude-code`, `claude-desktop`, `headless`; roles: worker/reviewer/orchestrator/coordinator; safe IDs; `boards=registry|home|comma-list` | Managed local host config; plan/diff/apply; host restart is prompted, not implied | main editor |
| `token_file`, `ca_file`, `bridge_command`, `config_path`, `seat_dir`, `repository`, `personal_command`, `token_env_var` | Safe path/command checks in planner; examples must use `/PATH/TO/...` | Private paths remain server-side; token content/fingerprint never enters UI | main editor, advanced grouping needed |
| `tier_max`, `skills`, `can_review`, `can_work`, `model`, `provider` | Tier `1..3`; safe unique skill IDs; role/capability combinations fail closed; model/provider ≤200 chars | Written to host connector environment and advertised at onboarding; restart/reconnect required | main editor |
| `bridge_name`, `board_connector_name`, `host_mode` | Safe connector IDs; `host_mode=acp|persistent` | Host configuration; restart required | `host_mode` is main schema but full ACP runner selection is pending PR 70 |
| Persistent runtime | `name`, `provider`, `base_url`, `model`, write-only `api_key`; providers include DeepSeek, Qwen, OpenRouter, Azure, Ollama, custom | Local runtime file plus OS keychain; test/start/stop/restart are explicit | main editor |
| ACP runner catalog/preset/install | runner ID, binary/source, session options, provider/model/mode selection | PR 70 introduces bounded catalog/installer; not in baseline | pending; never label shipped |

### Dispatch, board policy and Central retention

| Group | Fields | Authority / apply | Coverage |
| --- | --- | --- | --- |
| Dispatch | `claim_ttl_s` and `offer_ttl_s` `1..86400`; `broadcast_reoffer_s` `60..86400`; `second_opinion`, `fallback_broadcast` booleans | Board admin; Central setters; dynamic | main editor |
| Review | `review_policy` currently `strict` in project onboarding | Board admin; Central `board_review_policy_set` | no general dashboard editor; missing backend wrapper |
| Staleness | `stale_after_days` default `3` | Central `board_stale_after_set` | read-only in dashboard; missing writer |
| Archive/history | `archive_after_days` default `2`; `inline_history_limit` default `50`; `invite_prune_after_days` default `7` | Central board config; operational consequences | read-only; no dashboard writer |
| Journal | `journal_retention_days` default `7`; `journal_row_cap` default `50000` | Central `board_journal_retention_set`; compaction/retention apply dynamically | read-only; missing writer |
| Response/progress | `response_view=compact`; `ticket_progress_v1=false`; intake rate limit default `10/hour` | Central board config | read-only/internal; progress toggle requires migration decision |

### Butler provider and policy

Sources: `butler_settings.py:893-1234` and the `board_butler` validator.

| Group | Fields | Authority / validation | Coverage |
| --- | --- | --- | --- |
| Provider | `endpoint`, `model`, write-only `api_key`, `extra_headers`, `key_header`, `key_prefix`, `validation_path`, `draft_path`, `draft_protocol`, `answering_mode`, `expected_sha256` | HTTP(S), no URL credentials/query/fragment; non-loopback requires HTTPS; secret header values forbidden in readable fields; model validated with one bounded call; settings CAS | main editor |
| Policy layers | `board_butler.global`, optional `projects.*`, `boards.*` | Stored inside coordinator config with CAS; inheritance is product-owned | validator exists, no field-level dashboard editor |
| Mode/scope | `mode=shadow|active`, `answering_mode=off|assist|autonomous`, per-class `answer_scope` | `scope_change`, `gate_waiver`, `release`, `membership`, `registry` can never auto-answer | missing UI |
| Evidence/limits | `required_evidence_kinds`, `ceilings.per_hour|per_ticket|per_board`, `hold_before_post_s`, `active_windows` | Bounded IDs/counts, IANA timezones; active answering requires complete guards | missing UI |
| Safety | `kill_switch`, `auto_demote.veto_count|failure_count|window_s` | Kill available; active requires explicit authorization outside ordinary save | kill present; remaining UI missing |
| Task providers | `classification` and `drafting`: `model`, `endpoint_ref`, `key_ref`, headers and relative paths/protocol | References only; secrets stay in managed files | missing UI |

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
| Membership/invites/roles/scopes | Central invite/membership tools | read-only; no complete guided dashboard flow |

## Operational feature coverage

| Domain | Source symbol/file | Current capability | Redesign surface | Status / target package |
| --- | --- | --- | --- | --- |
| Ticket lifecycle | `BoardClient.ticket_create/claim/update/progress_update/annotate/submit/review/cancel/list/history_list` in `packages/client/src/pursers_client/client.py:1206-1663` | Dashboard reads all states/evidence; intake and human decisions write; ordinary ticket work remains authenticated-agent tooling | Work, ticket detail, Inbox | main read; route/Inbox package `TK-925e8c4b9657ebc5f5e5` |
| Questions and human requests | `ticket_question_ask/answer`, `ticket_request_human/human_resolve` | Human resolution and typed content are writable; generic conversation reply is not invented | Inbox | main; typed unification package |
| Review independence | Central review principal checks and delivery policy validation | Reviewer actions are not exposed as operator substitutes | Work/detail | main guard; retain |
| Journal/search/pagination | board detail views, activity projection, ticket list/history pagination | Bounded windows, cursors and truncation | Activity/search | main read; no unbounded Home fan-out |
| Attention | `activity_visibility.py`, local attention state | Dismiss/restore presentation only; does not resolve source item | Home/Inbox | main |
| Usage/overhead | `read_overhead_stats`, `/api/overhead` | Source/window-aware diagnostics | Activity/Diagnostics | main read; cost remains unknown unless observed |
| Resources/prompts/skills | Central resources and MCP tools; seat skill suggestions | Resource state is tool-visible; seat skills are editable; no complete dashboard catalog/onboarding | Settings > Resources & skills | gap → `TK-da4fa65758fe7e6e8f1e` plus backend package if mutation required |
| Sonar/UnifiedMCP | No complete baseline dashboard schema/handler for connector source, grouping, dedup, observations, zero-issue gate or writeback | Any displayed status would be documentation-only | Settings > Sources | missing backend → `TK-09b833b7aa9cca640ac6` |
| Doctor/import/bridge | `SeatConfigManager.doctor/import_discovered/install_bridge/upgrade_all` in `tools/fleet-dashboard/fleet_dashboard.py:7769-9327` | Guarded jobs and bounded logs | Settings > Diagnostics | main |
| Release/stage/restart | `tools/fleet-dashboard/release_ops.py`, `POST /api/config/ops/plan` and `POST /api/config/ops` | Exact expiring plan then async job; rollback/status projected | Settings > Release | main, high-risk grouping |
| Rollback | Release plan receipts and release status | Operation-specific, not a generic rollback button | Settings > Release | main partial; never shell shortcut |
| Personal profile/app setup | `packages/personal/src/pursers_personal/cli.py` | CLI supports setup/activate/doctor/rotate/restart/rollback/uninstall | Guided onboarding | CLI/file-only; dashboard scope needs explicit product decision |

## Static versus executed evidence

This inventory is a static source audit plus a read-only fetch of PR 70. No configuration, runtime, Door, repository policy or production save was executed. Field validators, handler registration and frontend calls were compared directly. Product-produced responses must still be exercised by the implementation tickets with isolated Central/registry/host fixtures; fabricated expected-value observations are not acceptance evidence.
