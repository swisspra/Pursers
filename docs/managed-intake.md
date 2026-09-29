# Managed source intake and autonomous runtime

Pursers can read external findings, let Butler decide which work to admit, create
source tickets, and open a pull request after independent approval. Local Git
performs checkout, edits, tests and push. MCP performs source reads and PR creation.
The native resident no longer requires a deployment-specific backend subclass.

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

All writeback paths record `delivering` before the external mutation. A timeout,
crash or failed completion annotation leaves an uncertain attempt for operator
reconciliation; it is never retried automatically, including when source revisions
change. Confirm whether a PR exists before changing that state. Writeback runs
before intake gating, so zero issues or no remaining capacity does not prevent
approved delivery. The worker's submission uses
`branch_and_commit: pursers/<ticket_id>@<full-40-hex-sha>`.

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
endpoint. Unknown or unavailable evidence cannot authorize unsafe scale changes.
The collector uses platform service adapters and the supported executor store
interface, then atomically writes fleet, registry-readiness and lease documents.
A busy seat on any selected board is protected across all boards. Missing, stale,
truncated, mismatched or retired identity evidence is treated conservatively.
The 120-second freshness deadline begins before collection, not after slow probes.

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
model/provider for generated board metadata. Configure the executor's approved
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
