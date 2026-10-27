# Resident VM operations

## Runtime and configuration

Check the active service arguments and the resolved Git checkout before trusting a version label. The private fork retains product version 5.2.1; runtime source and imported coordinator must belong to the same deployed cohort. The stable compatibility prefix is `$HOME/services/pursers-experience-aeb09e12`. Keep real endpoints, credentials, databases and customer mappings outside source control.

| Input | Current location or authority |
| --- | --- |
| Central registry and project ownership | `project_registry` and `project_coordinators` state on the control board |
| Connector/source declarations | `$HOME/services/pursers/config/connectors/butler-connectors.json` |
| Sonar mapping and default tier | `$HOME/services/pursers/config/intake-onboarding.json` |
| Templates/providers | `$HOME/services/pursers/config/local-fleet.json` plus approved Central envelope |
| Repository credential references | `$HOME/services/pursers/config/git-credentials.json`; use its approved exact-repository helper binding, never raw credentials in prompts or Git URLs |
| Source index, cursors, executor and observation state | Discover exact file arguments from the resident unit; preserve their contents and permissions |
| Service manager | `systemctl --user`, not the system manager |

Ordinary authorized automation should continue without adding new manual approvals. Configuration files alone do not grant new authority, activate a saved draft, or update Central registry state. Preserve expected revisions and policy digests when applying an authorized change. Keep signed executor bindings and template command strings intact.

Worker event-runner Git helper settings do not automatically apply to resident Butler. Before integration, verify that the registered fleet clone resolves its existing approved credential reference through the supported helper, then check a read-only ref and the configured Unified repository-details tool against the exact mapped repository/branch. Keep Git access, connector access and successful PR publication as separate observations. Do not copy worker credentials or change global Git configuration to make a repository-specific operation succeed.

A read-only check may inspect unit status, selected non-secret arguments, bounded MCP status and immutable receipts. Never dump complete environments or credential files. For direct storage diagnosis use SQLite `mode=ro` and `PRAGMA query_only=ON`; `CentralService.load()` can migrate state and is not a read-only inspection method.

For code submissions, distinguish the tested isolated worktree from the relay's
`ticket_submit(repository=...)` signing clone. If the authorized worktree lies
outside `--repository-root`, supply its owning registered clone within the
existing boundary after verifying repository/origin identity. The current
remote-tip preflight checks the exact branch and full SHA with `git ls-remote`,
not that clone's checked-out HEAD. Keep implementation in the worktree; never
switch the shared checkout, borrow a credential, or widen the boundary to the
home directory to submit. Independent exact-source review remains mandatory.

## Worker checkpoint communication

Coordinate checkpoints through operations the worker actually holds. Ordinary
workers lack `board:coordinate`, so `ticket_annotate` can be denied. Use a
worker-owned `ticket_question_ask` with `kind="information"` to send the pushed
SHA, results and relay request without pausing work. The coordinator verifies and
records an attributed annotation; no new worker scope is needed. Reserve human
holds for actual missing decisions or exhausted authorized recovery. Report full
cumulative source inventory separately from the owned delta; component approval
does not certify unfinished inherited workflow code.

## Capacity and runner selection

At the 2026-10-25 verification, the envelope permitted 20 total role seats, with ceilings of 16 workers, 4 reviewers and 0 ACP workers. The approved fleet used Goose event templates. Codex CLI connected as an interactive coordinator; that did not make it a fleet worker. These are dated observations, not defaults to reapply. Read the current envelope and local templates before changing desired seats or recommending ACP.

The host cap, current desired counts, active authorization, source `max_in_flight`, review backlog, provider capacity and file locks are distinct limits. Do not convert a 20-seat ceiling into 20 LLM rounds per hour. Do not sum the same cross-board seat more than once or terminate a worker holding a live lease.

## Intake and model boundaries

The live source was `sonar-all` with an explicit canary project and a 15-task grouping in-flight limit, despite wider PAT visibility. Sonar onboarding used tier 2 while the control-board default was tier 1. Preserve those settings unless a change is requested. A successfully observed source outside active scope is not permission to expand the canary automatically.

Severity filtering is temporarily combined because the Unified connector's severity mapping is not reliable enough for separate severity sources. Use the configured source and returned rule/type/severity evidence; do not guess connector enums or manufacture severity-specific queues.

The grouping implementation in `tools/board-butler/source_grouping.py` buckets rule/file/message occurrences for planning, preserves all source IDs, and reuses an unchanged snapshot. It still chunks accepted plans at 12 issues, 3 files or a 1,700-character rendered body. Those are current implementation limits, not a claim that every repair should have that size. A skill cannot remove them; a requested algorithm change requires a separately tested code change.

Model-facing ticket lists default to summaries and cursor continuation. Operational Butler/coordinator projections explicitly request work fields and hydrate incomplete rows. Full retained details remain available through `ticket_get(view="full")` and `ticket_history_list`. Keep pagination completeness, omission counts and retained-history gaps explicit. A list stub missing `tier` is not proof of corrupt stored data.

Paid intake receives source/queue/capacity counts; grouping receives compact candidate buckets and occurrence indices; question drafting receives selected question and evidence fields. Do not serialize all tickets or histories into those prompts. Current input budgets are 12,000 bytes for intake/question drafting and 32,000 bytes for grouping. These are not token counts. Ticket-description capacity remains 5,000 characters and board-state capacity 262,144; raising storage capacity does not fix a bulk-list response.

The direct model backend returns a constrained proposal; code performs validated connector/executor operations. It does not give the model an unrestricted MCP tool loop. Prompt changes are code/runtime changes; a skill file is not loaded into that backend automatically.

## Delivery differences

Resolve project policy instead of copying another project's route. At verification, backend delivery used an owned integration branch with `auto_integrate=false`; frontend used a legacy direct/per-ticket policy with manual final merge. The intended aggregate handoff does not make those observed configurations already aggregate.

For `batch_pr` or `branch_only`, require a matching active `delivery_policy_activation` and exact compiled policy revision. A saved draft or unsupported resident trigger stays inactive. The current resident supports the `ready` trigger; core support for manual/scheduled triggers does not prove their resident lifecycle is wired.

Integrate only independently approved exact SHAs into a verified fleet-owned clone and owned branch. Preserve ledger reservations, compare-and-swap ref checks and cumulative validation of the current head. A changed target invalidates prior validation. Freeze immutable snapshots; use rolling updates only when explicitly permitted and correlated to the same batch. A busy customer PR slot queues later delivery; it need not idle independent workers.

After a timeout or disconnect, reconcile the recorded operation and remote ref/PR before retrying. An approved ticket, source PR, consolidated PR and customer merge are separate states. Do not close Sonar issues or declare a scan clean just because ticket work or PR publication finished.

## Diagnose the failing boundary

| Evidence | Next check |
| --- | --- |
| `provider_unavailable` | Provider protocol, health, bounded response and recorded backoff; do not confuse it with a GitHub/ADO HTTP failure |
| Projection missing fields or incomplete pages | Exact ticket detail, application cursor and operational view; preserve stored tiers |
| `principal is not a member` | Principal admission on that exact board; seat join cannot fix it |
| Agent membership or generation conflict | Exact client seat name and existing process; do not take over the daemon identity |
| Question inbox ownership denial | Registered project coordinator; role alone does not confer ownership |
| `needs_human` | Its explicit requirement, mapping or test-baseline decision; infrastructure recovery does not answer it |
| Delivery blocked or outcome unknown | Activation, owner, exact evidence/refs, ledger reservation and remote reconciliation |

Keep independent projects running when one route is blocked. Service restarts, live configuration changes, credential actions and merges require the current operator request and effective authorization. For an authorized deployment, test the exact commit, back up consistent SQLite plus private config/state, preserve active leases, retain rollback, and verify both intake routes after cutover.
