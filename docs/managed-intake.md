# Managed source intake and autonomous runtime

Pursers can read external findings, let Butler decide which work to admit, create
source tickets, and open a pull request after independent approval. Local Git
performs checkout, edits, tests and push. MCP performs source reads and PR creation.
The native resident no longer requires a deployment-specific backend subclass.

Central emits `board_state_changed` when `project_registry`, `coordinator_intake`,
or `coordinator_config` values change. The event identifies only the state key;
subscribed coordinators reread authorized state and discover new project boards.
Identical writes and findings refreshes emit no routing event. Deploy Central and
the coordinator's client together to enable this event contract; older clients
may filter the new event kind and leave intake waiting for an unrelated event.

## Choose the operator interface and runner

Zed is the primary GUI/IDE workflow for this integration. Use the existing
[Zed guide](guides/zed.md) and [first-ticket walkthrough](guides/zed-first-ticket.md)
to connect, inspect work, answer questions and follow delivery evidence.

Goose is the CLI runner used by the event-driven worker/reviewer seats because
it fits unattended command-line execution. Its selection does not make Goose the
primary GUI or require operators to use its desktop interface. Zed and the CLI
fleet share Pursers Central and its authorization, ticket and review contracts;
this integration adds no new Zed UI or automatic GUI seat launcher.

## Ownership and prerequisites

Use Python 3.12 and one tested source revision for Butler, coordinator and executor.
Keep service configuration, credential files and state outside the checkout.
Credential files must be owned by the service account, regular files, and mode 0600;
state directories should be mode 0700. Do not put credentials in remote URLs,
command arguments, prompts, tickets or checked-in examples.

You need a Central principal authorized for the configured boards, a connector
credential, access to the source projects, and repository access for clone/push/PR.
A successful MCP connection does not grant Azure DevOps permissions. Missing or
expired credentials remain an external prerequisite. Preserve WORK and personal
project boundaries and resolve duplicate repository names explicitly.

The coordinator's separate intake credential needs `board:read`, `board:intake`,
and `board:coordinate`, with no `board:write`. It joins admitted projects as a
non-working coordinator before creating a generation-fenced ticket. Admission
must grant that credential's principal membership, separately from the main
coordinator principal when they differ.

## Source counts and model use

Add `observation` to each source in the connector configuration:

```json
{
  "observation": {
    "read_tool": "sonar_search_sonar_issues_in_projects",
    "arguments": {
      "projectKeys": "example-project",
      "issueStatuses": "OPEN,CONFIRMED",
      "pageIndex": 1,
      "pageSize": 1
    },
    "count_path": "paging.total",
    "max_age_s": 120
  }
}
```

This is a source fragment, not a complete connector configuration. Declare the
observation tool as read-only. Its arguments must select exactly the same projects
and issue statuses as the source's `fixed_args`. Validate argument types against
your server's discovered schema. Omit severity to read all severities when the
server's severity translation is incompatible; Pursers does not silently translate
enums. Keep the existing source ID stable when changing filters.

Every enabled source must return a fresh nonnegative integer count. If every count
is zero, Butler returns `no_open_issues` without resolving model credentials or
calling the model, including after restart. Missing/failed/malformed observations
are unknown, not zero. Counts expire after at most 120 seconds. Observations still
use connector schema, timeout, concurrency, rate and output limits.

For nonempty or unknown sources, the configured Butler model decides whether to
pull, how much, and source order. Code enforces the host seat ceiling. Successful
decisions are reused while semantic inputs and provider settings stay unchanged;
observation timestamps alone do not invalidate them. Provider failures stop intake
and back off for 15 minutes. Board refresh and read-only checks can continue during
that period. Decision caching is process-local; the issue index is durable.

Decision records expose `model_called`, `cache_reused`, `reason`, `model`,
`elapsed_ms`, and the provider's `provider_response_id` when available. Failed model
calls expose `retry_after`. Cached/empty decisions do not fabricate response IDs.
A timeout can still correspond to upstream model work: use the gateway's own logs
when no response ID was returned. Prompt bodies and credential headers are excluded.

## Approved delivery and remote SHA preflight

The source `writeback` block can declare this preflight for an MCP server exposing
`ado_repository_details_get` with `includeRefs`:

```json
{
  "on": "approved",
  "tool": "ado_pull_request_create",
  "arg_template": {
    "repositoryId": "{repository_name}",
    "project": "{repository_project}",
    "sourceRefName": "refs/heads/{source_branch}",
    "targetRefName": "refs/heads/{target_branch}",
    "title": "{ticket_title}",
    "description": "{ticket_id} approved at {approved_sha}"
  },
  "preflight": {
    "read_tool": "ado_repository_details_get",
    "arg_template": {
      "repositoryId": "{repository_name}",
      "project": "{repository_project}",
      "includeRefs": true,
      "includeStatistics": false,
      "refFilter": "heads/{source_branch}"
    },
    "repository_url_path": "repository.remoteUrl",
    "refs_path": "refs.value",
    "name_path": "name",
    "sha_path": "objectId"
  }
}
```

Declare the preflight tool as read-only and PR creation as an approved risky tool.
`ado_pull_request_create` requires preflight. The registered repository and target
branch must match the rendered PR arguments, and exactly one remote source ref
must match the approved full SHA. Other servers can use different declared tools
and response paths. A missing branch, moved head or mismatched target blocks PR
creation. Prevent concurrent writes to an approved branch: the read and PR-create
requests are separate upstream operations, not an atomic branch lock.

All writeback paths record `delivering` before the external mutation. For Azure
DevOps, also declare `ado_pull_requests_list` as a read-only connector tool. Butler
checks all returned pages for the exact repository, source branch, target branch
and approved SHA before creating a PR. An existing active or completed PR is
recorded as delivered without creating another. Abandoned PRs, a moved source SHA,
ambiguous matches, failed reads and incomplete pagination block creation.

Lookup and preflight failures are retried after at least 60 seconds, without an
LLM call. After a create timeout, crash or failed completion annotation, Butler
reconciles the existing PR automatically. If lookup still finds no PR, the state
remains `pr_uncertain`: an empty read cannot prove that an earlier mutation failed.
It does not repeat that create. An operator must establish the upstream outcome
before resetting the private attempt state. Other connectors retain the durable
once-only mutation guard. Preserve the private index across upgrades.

Writeback runs before intake gating, so zero issues or no remaining capacity does
not prevent approved delivery. Ticket evidence and the dashboard distinguish
`pr_pending`, `pr_blocked`, `pr_uncertain` and `pr_created`; legacy completion
markers appear as delivery recorded without inventing a PR identifier.

### Submission evidence and repeated review

The worker helper writes `branch_and_commit: pursers/<ticket_id>@<full-40-hex-sha>`.
Whitespace around `@` is accepted when reading older submissions. Conflicting or
malformed identities fail validation. Central verifies the provided remote-tip
proof and persists `branch`, `commit_hash`, `test_output` and `submission_preflight`
with the submission, including legacy `commit_hash` tickets. Both labeled
`test_output: ...` and inline `test_output=...` evidence are extracted; quoted
test summaries retain their semicolons. Reviewers still check
the exact code and test evidence; spacing alone is not a rejection criterion.

Two consecutive retryable rejections of the same SHA with the same feedback
(case and whitespace normalized) park the ticket with a workflow blocker. The
coordinator cannot offer it and workers cannot claim it while parked. This avoids
repeating the same model work; it does not approve the candidate. Changed SHA or
changed feedback is not caught by this guard. Older review records without a
fingerprint establish a new baseline on the next review.

Inspect the submission validator, review evidence and recorded blocker, resolve
the cause, then use the existing authorized `ticket_update(parked=false)` operation
to resume. Resuming clears the blocker. This feature does not change seat capacity
or impose an hourly model-run budget.

## Native local fleet evidence

File-based observation remains the default. Enable native collection with:

```text
--fleet-observation-mode local
--fleet-local-config /PATH/TO/config/local-fleet.json
--fleet-executor-config /PATH/TO/config/executor.json
--fleet-executor-state /PATH/TO/state/executor
--fleet-observation-file /PATH/TO/state/butler/fleet-observation.json
```

The private local-fleet configuration binds every approved executor template:

```json
{
  "templates": {
    "worker-template": {"board_id": "pursers", "provider": "worker-model"}
  },
  "providers": {
    "worker-model": {
      "endpoint": "https://gateway.example/v1",
      "model": "example-model",
      "secret_file": "/PATH/TO/auth/worker-model.key"
    }
  }
}
```

Provider health checks read `/models`; they never issue chat/completion requests.
Bind worker/reviewer templates to their actual model, not merely Butler's drafting
endpoint. Provider budgets preserve each role's target and retain active holders;
a faster worker model cannot consume a reviewer-only model's allocation. Unknown or unavailable evidence cannot authorize unsafe scale changes.
The collector uses platform service adapters and the supported executor store
interface, then atomically writes fleet, registry-readiness and lease documents.
A busy seat on any selected board is protected across all boards. Missing, stale,
truncated, mismatched or retired identity evidence is treated conservatively.
Running seats require a recent registry heartbeat. Stopped seats can restart from
a fresh identity/membership read even when their last heartbeat is old; remote
leases still protect held work. The 120-second observation deadline begins before
collection, not after slow probes.

## Scoped Git credentials

Create a private configuration for `tools/ado-connector/git_credential.py`:

```json
{
  "repositories": [{
    "url": "https://dev.azure.com/example-org/example-project/_git/example-repo",
    "credential_file": "/PATH/TO/auth/ado.key"
  }]
}
```

The helper implements Git's `get/store/erase` protocol. Only `get` for an exact
configured HTTPS host and repository path returns a credential. Other targets,
ambiguous entries, malformed paths and unsafe credential files return none.
The deployment renderer and event runner can receive `git_credentials_config`;
they set process-local Git helper configuration with `credential.useHttpPath=true`.
They do not replace unrelated global Git configuration. Child clone/push commands
inherit the scoped helper.

The Git credential helper responds only through a pipe, as used by Git. Direct
terminal output and redirection to a regular log file return no credentials.
Never route the helper protocol stream into application logs.

## Render and launch

Start from [the deployment example](../tools/board-butler/examples/managed-intake.json):

```sh
python tools/board-butler/deployment.py \
  --config /PATH/TO/config/deployment.json \
  --platform systemd --output /PATH/TO/rendered
```

Use `--platform launchd` for macOS. Rendering writes owner-only service files but
does not install or start them. Review the rendered command and environment, then
install through your existing service procedure. State remains outside the source
checkout. The default mode is shadow. For active mode add `runtime_mode: active`,
`active_authorization_file`, `active_board` and the existing signed executor flags:
`fleet_state_file`, `fleet_executor_socket`, `fleet_executor_key_id`,
`fleet_executor_private_key`, `fleet_executor_config`, and the observation settings.
Use the existing authorization tools; do not manufacture or extend authorization.

The existing `tools/board-butler/launch.sh` also forwards these optional variables:

| Variable | CLI flag |
| --- | --- |
| `PURSERS_BUTLER_CONNECTOR_CONFIG` | `--connector-config` |
| `PURSERS_BUTLER_SOURCE_INTAKE_INDEX_FILE` | `--source-intake-index-file` |
| `PURSERS_BUTLER_INTAKE_ONBOARDING_CONFIG` | `--intake-onboarding-config` |
| `PURSERS_BUTLER_FLEET_OBSERVATION_MODE` | `--fleet-observation-mode` |
| `PURSERS_BUTLER_FLEET_LOCAL_CONFIG` | `--fleet-local-config` |
| `PURSERS_BUTLER_FLEET_EXECUTOR_STATE` | `--fleet-executor-state` |

Host seat caps, warm pools, template counts and authorization lifetime remain
operator choices. Cap 15 does not create 15 seat templates. Onboarding's
`default_ticket_tier` affects new boards only; it does not rewrite existing boards.

## Event-driven seats

`tools/seat-kit/event_seat.py --config /PATH/TO/config/seat.json` runs a bounded
Goose turn only after a relevant registry event. Supply private configuration:

```json
{
  "seat_id": "worker-example", "role": "worker",
  "provider": "configured-provider", "model": "example-model", "tier_max": 2,
  "seat_dir": "/PATH/TO/seats/worker-example",
  "board_script": "/PATH/TO/seats/worker-example/bin/board.sh",
  "state_file": "/PATH/TO/state/seats/worker-example.json",
  "token_file": "/PATH/TO/auth/worker-example.jwt",
  "goose": "/PATH/TO/bin/goose", "mcp": "/PATH/TO/venv/bin/pursers-mcp",
  "central_url": "https://central.example/mcp", "home_board": "pursers",
  "repository_root": "/PATH/TO/clones",
  "max_runs_per_hour": 5, "max_turns": 30, "turn_timeout_s": 1800,
  "git_credentials_config": "/PATH/TO/config/git-credentials.json"
}
```

Pass the same file as `--event-config` to `tools/seat-kit/seat_new.py` when generating
or upgrading that seat. It verifies seat identity/role and uses the configured
model/provider and `tier_max` for generated board metadata. Regenerate managed
seat files with `--upgrade` during migration; an old `board.sh` can advertise a
different tier and correctly fail readiness checks. Configure the executor's approved
command to invoke the event runner with that config; retain the executor's existing
provisioning, template digest and authorization checks.

New board cursors come from Central's authoritative snapshot watermark, never a
hardcoded value or cursor-zero scan. The runner freezes each wait's board selection
to that registry snapshot and refreshes the registry before the next wait. Saved
cursors and pending events survive restart. The hourly model-run budget stops the
runner with pending events retained; investigate before restarting. A run reserved
before an uncertain failure is not automatically replayed. This budget counts
Goose runs; `max_turns` separately bounds steps within each run.

## Migration, checks and rollback

1. Back up unit files, configuration, index, executor state, cursors and authorization
   references. Record the old source revision; keep all credentials private.
2. Preserve source IDs. The index keys include source ID and external issue ID.
   Removing an indexed source requires explicit migration; do not delete the index
   to force a retry or regroup severity sources with existing work.
3. Add native count observation, PR preflight and explicit local fleet bindings.
   Verify read-only tool schemas and repository mapping. Test branch access with
   scoped `git ls-remote`; keep valid tracking refs needed by approval scanning.
4. Render the native entry point, retain explicit active authorization, and converge
   Butler/coordinator/executor to one tested revision. Check authorization expiry,
   actual template count versus cap, service start limits and runtime findings.
5. On an empty-source canary, verify fresh observations and repeated
   `model_called=false`, with no duplicate intake or writeback. Test the workflow
   against real Central and fake external boundaries before enabling real delivery.
6. Real source-to-approved-PR acceptance requires an accessible open issue and a
   scoped target. Passing unit tests or observing an empty source does not establish
   this final external acceptance. Do not create production test PRs unnecessarily.

For rollback, restore the previous code and service configuration while preserving
the newest source index, delivery attempts and cursor state. Reverting those data
files could replay an uncertain external mutation. Do not combine source-ID or
index-format migration with this cutover. Publishing a package release remains a
separate release-train operation built from its tested tag.

## Discover projects and onboard fleet members

The optional onboarding policy below refreshes Sonar and Azure DevOps inventories
through declared read-only MCP tools. Exact unique repository names can match;
ambiguous names stay unresolved. Explicit mappings must still point to a repository
visible in the current PAT inventory. Record both the ADO project and repository,
exact target branch casing, and Sonar project/analysis branch. Grouped intake additionally verifies that the Sonar main analysis branch name and
commit match the exact configured ADO target ref before planning. Stale analyses or
branch mismatches admit no new work. This mode currently uses the main Sonar
analysis branch; non-main branch analysis requires a separate supported adapter.

```json
{
  "sources": {
    "sonar-all": {
      "domain": "work",
      "projects_root": "/PATH/TO/fleet/clones",
      "auto_onboard": true,
      "per_cycle_cap": 2,
      "retry_limit": 3,
      "retry_backoff_s": 300,
      "default_ticket_tier": 2,
      "discovery": {"kind": "sonar_ado", "refresh_seconds": 900},
      "member_roles": {
        "PR-worker": "member",
        "PR-reviewer": "reviewer",
        "PR-coordinator": "member",
        "PR-intake": "member"
      },
      "repositories": {
        "example_backend": {
          "repository_url": "https://dev.azure.com/example/Backend/_git/api",
          "integration_ref": "dev"
        }
      }
    }
  }
}
```

Use real verified principal IDs from `board_members`; agent names are not principal
IDs. The Butler credential needs `board:read`, `board:write`, and
`board:coordinate`, and must be allowed to bootstrap a new board. It joins initially
with work/review capabilities disabled, provisions the configured members and tier,
and publishes the fleet-owned clone in the project registry. Worker/reviewer token
scopes remain necessary; membership does not grant token scopes. The coordinator
intake principal additionally needs `board:intake`. No existing board admission is
silently elevated. Bootstrap, clone or admission failures back off and leave the
project unregistered for intake.

Inventory refresh is bounded: at most 2,000 Sonar projects, 200 ADO projects, and
5,000 repositories. Incomplete inventory does not publish a partial scope. Keep the
Git credential helper's repository allowlist consistent with authorized discovered
mappings; inventory access alone does not authorize a clone or push.

## Group Sonar occurrences into repair tickets

Add this fragment to a paged Sonar source in `butler-connectors.json`:

```json
{
  "grouping": {
    "kind": "sonar",
    "canary_project": "example_backend",
    "max_in_flight": 1,
    "max_admitted_groups": 1
  }
}
```

`canary_project` limits the source snapshot and observation to one project during
discovery. `max_admitted_groups` is a durable total admission cap for this source,
including completed groups; one completed PR does not start a second canary.
Exhausted group limits skip source observation and intake model calls while the
independent delivery pass continues.
Raise or remove that cap deliberately after inspecting the first result. Remove
`canary_project` to cover all resolved projects. `max_in_flight` defaults to 15 and
is also bounded by the host seat ceiling and Butler's model-directed allowance.

Butler reads the complete paged snapshot before planning (at most 2,000 issues).
Rule, path and line metadata let its configured drafting model propose compatible
repair groups, objectives and validation. These are planning suggestions: the worker
must inspect actual code and preserve behavior, and the reviewer independently
checks the repair. Code validates that every issue appears exactly once, splits
oversized groups to at most 12 issues/3 files, and preserves all IDs in ticket text.
Severity is not the grouping key; related rules may share one repair, while unrelated
changes within a file may need separate tickets.

The planner first considers compatible small repairs within each file, including
different rules that share an objective and validation. Repeated edits across
functions need not become separate tickets. Behavioral, async/API, security,
exception-handling and mutation changes require their own compatibility assessment;
file identity alone does not justify combining them. Planner policy version 2
refreshes older cached plans once, then reuses the validated plan for unchanged
inputs. Already admitted issue membership and existing tickets/PRs are preserved;
only remaining occurrences can be admitted under the new plan.

Plans and their membership use private mode-0600 files next to
`--source-intake-index-file`. Unchanged snapshots reuse the durable plan across
restarts. Up to 12 omitted occurrences receive one bounded model repair request; the final
partition must still cover all occurrences exactly once. A failed plan backs off
for 15 minutes. Intake uses a durable prepared
reservation before the Central CAS write, so an interrupted write can recover the
same ask without losing or duplicating members. New analysis does not expand an
already admitted ticket. Capacity counts groups, not raw Sonar occurrences.

Groups sharing a repository, target branch and file are serialized while work or
PR delivery is active, including different Sonar keys mapped to the same repository.
Confirmed PR delivery releases the file hold so distinct issues in that file can
proceed on another branch without waiting for a human merge. Uncertain delivery
retains the hold. Delivered issue IDs remain reserved and cannot be admitted again.
Separate PRs touching the same file can require conflict resolution when merged;
delivery does not imply merge or Sonar resolution. Keep the index when deploying
or restarting: deleting it removes deduplication and delivery guards. Existing
delivered records release their holds automatically with this runtime update.

Add `{issue_ids}` to the configured writeback description to include every member
ID in the single approved PR. Existing remote repository/branch/SHA preflight and
once-only delivery protection still apply. Review approval and PR creation do not
prove Sonar closure; confirm a subsequent Sonar analysis after integration.

### Structured state capacity

Central accepts up to 262,144 characters per `board_state_update` value. This
separate storage limit accommodates multi-item intake queues and project
registries; it is not a model context budget. Ticket descriptions and submission
notes retain their existing limits. Oversized state writes fail atomically rather
than truncating queued work. Scrubbing, authorization and compare-and-swap
preconditions still apply. Read a specific state key when inspecting large queues.

Upgrade Central to obtain this limit; there is no deployment flag or database
migration. Retain existing intake state and the Butler issue index during upgrade.
A retry can publish prepared intake entries without creating duplicate tickets.

Intake decisions that report `provider_unavailable` include a bounded `error_class` and, for HTTP failures, `http_status`. The original metadata remains visible during the 15-minute backoff; raw provider messages and credentials are never included.

Intake decisions reserve up to 1,600 completion tokens, shared by model reasoning and the final JSON object. This is a per-response ceiling, not a fixed charge. Empty truncated responses report `provider_response_truncated`; malformed JSON reports `provider_response_invalid`. Both fail closed and retain the existing 15-minute retry backoff. Observed empty sources still skip the model, and unchanged decision inputs still reuse the cache.

When the provider reports token usage, intake audit metadata includes `provider_usage` with prompt, completion, total, and reasoning token counts. Successful cached decisions do not emit new usage. Failure metadata is retained during backoff: count usage only when `model_called` is true, using `provider_response_id` to deduplicate. Missing usage means unreported, not zero.

The intake model receives `fleet_load` with unique fresh `workers`/`reviewers` and
`idle_workers`/`idle_reviewers` across registry boards. Per-board memberships must
not be added together. A busy seat is excluded from idle counts but remains fleet
capacity for planning the next task. Butler chooses a small ready queue using open
work, in-flight work and review backlog; busy workers alone are not a reason to
leave the next-work queue empty. Pull counts remain model decisions within existing
seat and group ceilings. Empty-source skipping, decision caching and failure backoff
remain in effect. Routing-state events are visible through catchup to principals
with `board:coordinate`; workers retain their existing event visibility.

### Review evidence across repair commits

A submission's `files_changed` must list the complete candidate diff, including
review corrections in earlier commits. Generated reviewer helpers fetch the
registered project's `integration_ref` (default `main`), compute its merge-base
with the exact submitted SHA, and verify changed paths and credentials across
that whole diff. They print `integration-ref` and `verification-base` as evidence.
Missing or invalid integration refs fail verification; workers cannot supply an
alternative base in submission notes. Suite replay remains on the exact SHA.

When upgrading existing seats, regenerate their `bin/board.py` helpers and update
`pursers-client` together so registry `integration_ref` survives parsing. Preserve
seat identity, credentials and event cursors. Worker helpers validate required
`branch_and_commit` metadata before submission, including older tickets that
require only `commit_hash`, avoiding review cycles caused
by malformed evidence.

### Seat capacity versus model execution frequency

`host_seat_cap` and the executor's `host_cap` limit concurrent fleet seats.
A cap of 20 permits at most 20 seats; it does not limit a seat to 20 model
executions per hour and does not request 20 running seats. Existing role,
resource, board and template limits still apply.

`max_runs_per_hour` is an independent, optional event-seat throttle. Set it to
JSON `null` to disable that throttle. Omitting the field retains the legacy
default of 5; an explicit integer from 1 through 100 enables the hourly limit.
When enabled, exhaustion retains pending events and cursors, waits for the
rolling-hour window, and resumes without restarting or replaying completed
events. State exposes `rate_limited_until` while deferred. Per-execution
`max_turns`, timeouts, event deduplication and empty-source/cache checks remain
independent of this throttle. These are execution counts, not provider token
quota or billing limits.


### Fleet recovery and provider probes

A reachable model endpoint does not prove that a seat can work. The reconciler
keeps service health separate from provider health: an `unhealthy` process still
occupies hard host, board and role limits, but cannot replace usable capacity.
It is not automatically stopped when its identity is uncertain. A stopped seat must also have complete registry identity and membership evidence
on every selected board before it is eligible. A healthy, authorized stopped seat can fill the remaining capacity within those limits.

For recovery, an eligible seat with the most recent successful start is preferred
to an unused or older seat before comparing provider probe latency. A failed
provider probe prevents new starts on that provider; it does not turn a known
running seat into zero capacity or drain the configured minimum by itself.
Independent human maxima and approved-template checks still apply.

An idle `draining` seat finishes its stop after the configured grace period, even
when capacity is needed again. A subsequent observation can start it afresh.
Live leases and busy work prevent this stop. This avoids treating a draining
process as a ready replacement forever. No model requests are needed for this
reconciliation, and no seat or hourly execution limit changes are required.

### Optional hourly intake limits and quota recovery

Seat ceilings bound simultaneous work. Two independent settings bound ticket
creation over a rolling hour: coordinator `intake.rate_per_hour` (CLI
`--intake-rate-per-hour`) and each Central board's `config.intake_rate_limit_per_hour`.
Defaults remain unchanged. To run without hourly creation throttling, explicitly
set the coordinator value to JSON `null` or CLI `none`, and the Central board value
to `null`. The Central setting is persisted board configuration, not a coordinator
state key; a host administrator must update it with the supported Central runtime
and a database backup. Deploy the nullable-limit runtime before setting it. Other
boards retain their own limits. Do not change tokens or add `board:write` to the
intake principal to bypass a limit.

A Central hourly-limit refusal leaves the ask queued, defers that board for at
least 60 seconds and retries on a subsequent coordinator cycle. It does not count
as a failed creation or trip the permanent creation breaker. Other creation errors
retain their existing breaker. A coordinator process that already tripped the old
breaker needs a controlled restart after deployment; its durable ask IDs preserve
idempotency. This changes neither worker model-run throttles nor host seat caps.

### Provisioning shared fleet capacity

`host_seat_cap` limits concurrent agent processes; it does not create runners.
To make a larger fleet available, provision an independent identity, role-scoped
credential, seat directory, event configuration and cursor file for each runner.
Generate its instructions with `tools/seat-kit/seat_new.py --event-config`, admit
its principal to every active registry board, and bootstrap a positive cursor
before making it eligible for starts. Reviewers require independent principals.
Keep worktrees separate; existing lease holders and their cursor files must survive
capacity changes.

Add each runner to the executor templates and local fleet bindings. Use a shared
provider binding for runners on the same endpoint/model so a capacity increase does
not multiply provider discovery requests. Update the authorized template list and
role maxima, desired role minimum/target/maximum, board/host concurrency and executor
policy together. Recompute the envelope fingerprint and use a fresh, authorized
config revision. For example, a host cap of 20 can bound 16 workers and 4 reviewers;
it does not require all 20 to run when there is no demand or host headroom.

In local observation mode the shared home fleet includes demand from active registry
projects without their own fleet policy. Projects with their own policy remain
separate and are counted once. An incomplete project scan prevents scaling decisions.
A truncated ticket payload is acceptable only when the complete coordination scan
is available; missing membership or identity evidence still prevents starts.

After provisioning, verify eligible stopped inventory, autonomous start receipts,
registry memberships and real work/review claims. Service status alone does not prove
that a runner can accept work. Keep a private configuration backup and document the
actual running count separately from the host ceiling.

The event runner's `repository_root` is the relay's authorized boundary. It must
contain the canonical active project clone paths from the registry, including
`fleet_clone_dir` overrides. A per-seat checkout directory is insufficient when
registry work is routed to a shared projects directory. Use the explicitly approved
host projects root and retain isolated ticket worktrees within it. Bootstrap checks
resolved paths, including symlinks, and rejects a mismatched boundary before a model
is called. It does not expand permissions automatically. Change private deployment
configuration through the operator, then roll idle runners without interrupting live
work/review leases; preserve their cursor files.

Local fleet observation attributes active work and review leases to their holder.
An unidentified active holder remains protected conservatively. Other ready seats
can drain and stop when demand drops after the existing grace/cooldown period.
