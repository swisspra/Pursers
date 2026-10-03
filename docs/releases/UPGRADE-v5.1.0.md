# Upgrade to Pursers 5.1.0

Pursers 5.1.0 is a coordinated release of Central, the Python client,
Personal, the wait bridge, and the ACP integration. It adds bounded ticket and
history pagination, lifecycle activity, MCP help resources and prompts,
portable role skills, persistent agent display labels, faster Fleet first-open,
repository delivery settings with activated `batch_pr` and `branch_only`
routes, and opt-in macOS Codex event-seat autoscaling.

This guide upgrades an existing 5.0.9 installation without replacing its
durable board or fleet state. Installing packages alone does not migrate or
restart a running Central, wait bridge, Board Butler, dashboard, or seat.

## Compatible cohort

Install these versions together:

| Distribution | Version |
| --- | --- |
| `pursers` | `5.1.0` |
| `pursers-central` | `0.1.7` |
| `pursers-client` | `0.1.8` |
| `pursers-personal` | `5.1.0` |
| `pursers-personal-import` | `5.0.0` |
| `pursers-wait-bridge` | `0.1.6` |
| `pursers-acp` | `0.1.7` |

The importer remains at `5.0.0` because its wheel is byte-identical to the
published artifact. ACP advances because its dependency metadata pins the new
client, Personal, and wait-bridge cohort. PyPI versions are immutable; do not
rebuild a different wheel under an existing version.

## 1. Prepare and back up

1. Record the running package versions, service definitions, Central URL,
   active boards, and exact configured seat identities.
2. Stop new intake and scaling. Let current work finish, or preserve each live
   ticket's branch, full commit SHA, lease holder, and next action.
3. Back up the Central instance directory and its SQLite files using the
   existing instance backup procedure. Also back up the project registry,
   wait-bridge cursor files, seat state, Board Butler state, Fleet Executor
   state, delivery configuration, authorization envelopes, and service files.
4. Keep credentials outside the backup shared for review. Never copy token
   values into tickets, logs, or repositories.

Do not delete or recreate databases, ticket archives, journal cursors, lease
snapshots, delivery indexes, or source-intake indexes. The new pagination and
activity projections read the existing durable records.

## 2. Upgrade packages in a fresh environment

Use Python 3.12 for the controlled release check and install from one trusted
index or the verified release wheel set:

```sh
python3.12 -m venv /PATH/TO/pursers-5.1.0
/PATH/TO/pursers-5.1.0/bin/python -m pip install --upgrade pip
/PATH/TO/pursers-5.1.0/bin/python -m pip install \
  "pursers==5.1.0" \
  "pursers-wait-bridge==0.1.6" \
  "pursers-acp==0.1.7"
/PATH/TO/pursers-5.1.0/bin/python -m pip check
```

Point service definitions and generated seat helpers at the new environment,
then regenerate supported helpers rather than editing generated files by hand.
Upgrade Central, the client used by every relay, the wait bridge, Board Butler,
and managed runners before reopening intake. A mixed cohort can miss new
pagination, submission-evidence, activity, or discovery fields.

Existing `pursers-acp` remains the shipped IDE integration for Zed and other
ACP hosts. Zed does not need to remain open for headless event seats. ACP
Registry publication and reusable native-host presets are future design work,
not 5.1.0 features, and native application profiles are not portable fleet
credentials.

## 3. Restart and verify in order

1. Start Central against the preserved instance and verify `/healthz` before
   admitting clients.
2. Start one administrative client and confirm the instance identity, board
   registry, ticket counts, archived-ticket access, and latest journal cursor.
3. Start the 0.1.6 wait bridge and verify a positive saved cursor resumes by
   push. Never reset a cursor to zero to test connectivity.
4. Start Board Butler and the Fleet dashboard. Confirm the first Home response
   is a bounded per-board summary; slow enrichment may remain visibly pending.
5. Regenerate and start one worker and one independent reviewer. Confirm their
   returned principal, `agent_id`, role, capabilities, and all active WORK
   boards before restoring the rest of the fleet.
6. Re-enable intake and delivery only after configured repository routes and
   their exact mapped bases are correct. Existing in-flight pull requests and
   branches retain their recorded route.

Exercise ticket list and history continuation using the returned opaque
cursor. Cursors are principal- and filter-bound; do not decode, edit, or reuse
one with different filters. Verify ticket activity is a projection of durable
state, not a substitute for ticket history or submission evidence.

## 4. Configure macOS Codex autoscaling (optional)

Codex event-seat autoscaling is opt-in. It uses the existing signed Fleet
Executor boundary; Board Butler cannot start or stop a process directly.
Provisioning stages owner-only files and reports launchd actions for the
operator, but does not call `launchctl`:

```sh
python3 tools/seat-kit/fleet_executor_provision.py plan \
  --spec /PATH/TO/provision-spec.json \
  --output /PATH/TO/provision-plan.json
python3 tools/seat-kit/fleet_executor_provision.py confirm \
  --plan /PATH/TO/provision-plan.json \
  --confirm APPLY-<digest-from-plan>
```

The provision spec must bind explicit repository and seat roots, credential
references, distinct worker and reviewer principals, board caps, a host cap,
and the approved templates. Keep `min`, `target`, and `max` capacity within the
signed envelope; set scale-up, scale-down, and failure-backoff cooldowns; and
configure load, memory, and disk headroom. Scale-down is fail-closed when any
selected board has a live work/review lease or incomplete, stale, or ambiguous
lease evidence.

Each Codex event-seat config uses `client: "codex"`, an absolute `codex`
executable, `model`, `effort`, `service_tier`, `codex_sandbox`, seat and state
paths, and `repository_root`. The runner preserves the service environment's
`CODEX_HOME`. Add `codex_profile` only when that exact named profile exists in
that home; otherwise omit it. A native host profile is not copied into another
seat automatically.

Local readiness verifies the executable plus private auth/profile files. It
does not probe or assert remote subscription quota: Codex quota remains
`unknown`. Idle capacity observation and push waiting make no model calls; a
model starts only for an authorized offer or resumable owned ticket.

### Hand off from a legacy controller

Never run two controllers for the same seats. First create a private migration
spec that names the signed executor config, local config, all-board lease
snapshot, legacy supervisor command and PID file, and controller marker. Then
preview and confirm:

```sh
python3 tools/seat-kit/codex_fleet_migration.py preview \
  --spec /PATH/TO/migration-spec.json \
  --output /PATH/TO/migration-plan.json
python3 tools/seat-kit/codex_fleet_migration.py confirm \
  --plan /PATH/TO/migration-plan.json \
  --confirm CONFIRM-<digest-from-plan>
```

Preview must identify exactly one legacy supervisor, reject duplicate or
ambiguous Codex processes, preserve disabled seats, and show no live lease on
any selected board. Confirmation writes only the controller marker and performs
no live action. The operator separately stops the legacy supervisor, verifies
only idle enabled seats are stopped, activates the staged service definitions,
and confirms there is still exactly one controller.

The release evidence covers startup floor and stable-idle behavior. It does not
claim a live backlog scale-up/scale-down run against a real subscription.

## 5. Delivery and skills

Repository delivery policy is inherited from global defaults through named
groups to repository overrides. Preview the effective field provenance and
apply with the current revision. `batch_pr` and `branch_only` are active only
after explicit supported activation; unsupported triggers remain inert. Never
retarget an in-flight delivery implicitly.

Portable `pursers-start`, `pursers-work`, `pursers-review`, and
`pursers-operate` skills can be installed with the bundled dry-run-first
manager. They contain workflow instructions only: they do not configure
credentials, grant board roles, or widen authorization. See
[Portable Pursers skills](../guides/portable-skills.md).

## Rollback

Stop new intake and scaling, preserve any new ticket/branch evidence, and stop
the 5.1.0 services. Restore the backed-up service definitions, environments,
Central instance, cursors, executor state, and delivery/index state as one
cohort. Do not point 5.0.9 services at a database or cursor set that has been
partially restored, and do not delete tickets created during the attempted
upgrade; export or reconcile them deliberately.

If only process startup failed and no durable writes occurred, keep the
preserved data and switch service definitions back to the prior environment.
If Central wrote new state, use the verified backup or an operator-approved
forward fix. Never roll back by republishing different bytes under an existing
PyPI version.

After rollback, verify Central health, instance identity, board and archived
ticket counts, journal continuity, cursor positions, live leases, registry
projects, delivery routes, and one push wait before reopening intake.
