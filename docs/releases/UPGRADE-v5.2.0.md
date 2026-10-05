# Upgrade to Pursers 5.2.0

Pursers 5.2.0 is a coordinated release of Central, the Python client,
Personal, the wait bridge, and the ACP integration. It adds the Nocturne Fleet
workflow routes, guarded Settings families, managed ACP Registry runner setup,
the supplied Pursers brand, lower-amplification Central state writes, and
lease-safe transient transport recovery.

This guide upgrades an existing 5.1.0 installation without replacing durable
board, registry, cursor, or fleet state. Installing packages does not migrate
or restart a running Central, wait bridge, Board Butler, dashboard, or seat.

The release includes the independently reviewed Settings layout fix and a
required Chromium layout and interaction gate covering narrow, desktop, and
200% zoom layouts. Use the verified release artifacts and confirm the deployed
dashboard revision after restarting the service.

## Compatible cohort

Install these versions together:

| Distribution | Version |
| --- | --- |
| `pursers` | `5.2.0` |
| `pursers-central` | `0.1.8` |
| `pursers-client` | `0.1.9` |
| `pursers-personal` | `5.2.0` |
| `pursers-personal-import` | `5.0.0` |
| `pursers-wait-bridge` | `0.1.7` |
| `pursers-acp` | `0.1.8` |

The importer remains at `5.0.0` because its source is unchanged from the
published artifact. Central, Client, Personal, Wait Bridge, and ACP all advance
because their source, bundled resources, or dependency pins changed. PyPI
versions are immutable; do not rebuild different bytes under one of these
versions.

## 1. Prepare and back up

1. Record the running package versions, service definitions, Central URL,
   active WORK boards, exact seat identities, and current project registry.
2. Stop new intake and scaling. Let active work finish or record each ticket's
   exact branch, full commit SHA, lease holder, positive cursor, and next
   action. Never reset a cursor to zero as an upgrade shortcut.
3. Back up the Central instance and SQLite files through the existing instance
   procedure. Also preserve the project registry, wait-bridge cursor files,
   seat state, Board Butler state, Fleet Executor database and policy, delivery
   configuration, authorization envelopes, and service definitions.
4. Keep credentials and provider account data outside any backup shared for
   review. Never place token values, private host names, or user home paths in
   tickets, logs, or the repository.

Do not delete or recreate ticket archives, journal cursors, lease snapshots,
delivery indexes, source-intake indexes, or runner selection locks. The 5.2.0
interfaces read and extend the existing durable state.

## 2. Install the complete cohort

Use Python 3.12 for the controlled release check and install from one trusted
index or from the verified seven-wheel release set:

```sh
python3.12 -m venv /PATH/TO/pursers-5.2.0
/PATH/TO/pursers-5.2.0/bin/python -m pip install --upgrade pip
/PATH/TO/pursers-5.2.0/bin/python -m pip install \
  "pursers==5.2.0" \
  "pursers-wait-bridge==0.1.7" \
  "pursers-acp==0.1.8"
/PATH/TO/pursers-5.2.0/bin/python -m pip check
```

Point service definitions and generated seat helpers at the new environment,
then regenerate supported helpers instead of editing generated files by hand.
Upgrade Central, the client used by relays and seats, the wait bridge, Board
Butler, Fleet dashboard, managed runners, and ACP before reopening intake. A
mixed cohort can lose new Settings contracts, runner selections, recovery
classifications, or response fields.

## 3. Restart and verify in order

1. Start Central against the preserved instance. Verify `/healthz`, instance
   identity, board and archive counts, registry state, and latest journal
   cursor before admitting clients.
2. Start one administrative client. Confirm a duplicate state write returns
   the existing state without moving its generation, while a stale generation
   or value precondition still fails closed.
3. Start Wait Bridge 0.1.7 from its saved positive cursor. Exercise one
   three-board push wait and a forced transient transport reconnect. The bridge
   must re-arm with the same cursor and must discard a closed client even if
   teardown fails. Authentication and configuration errors must not reconnect
   as transport failures.
4. Start Board Butler and Fleet Dashboard. Confirm idle refreshes preserve the
   bounded five-minute heartbeat without rewriting unchanged observation and
   Fleet state every cycle.
5. Start one worker and one independently credentialed reviewer. Confirm exact
   principals, agent IDs, roles, capabilities, and active WORK-board coverage.
   A transient timeout may recycle transport, but it must not duplicate a paid
   model turn or discard a live lease.
6. Re-enable intake, scaling, and delivery only after the exact registry,
   repository mappings, caps, and service revisions match the saved plan.

WORK and HOBBY/PERSONAL remain separate. Registry routing is limited to active
WORK projects. The macOS fleet ceiling remains an operator-configured limit;
it is not derived from the machine size or embedded as a private host value.

## 4. Verify Nocturne and Settings

Fleet now exposes source-backed Home, Inbox, Work, Team, Approvals, Projects,
and Settings routes. `#/inbox` is the canonical Inbox entry point while
`#/approvals` remains available. Ticket detail, lifecycle, submission, test,
file, review, and coordination-question views retain their server-provided
bounds; the browser must not infer missing evidence.

For every Settings family used by the deployment:

1. Select the exact Central and board. Duplicate board names must remain bound
   to their Central identity.
2. Read the effective state and revision before editing.
3. Preview the typed change. Verify redaction, immutable scope, restart
   semantics, unsupported fields, and expected revision.
4. Apply only the reviewed preview and current revision.
5. Read back the effective state and audit evidence. A save is not complete
   merely because the request returned success.

The guarded families cover connector/source onboarding, delivery, seats and
dispatch, membership, board policy, Central retention, Butler, diagnostics,
and managed runner setup. Central retention apply changes configuration only;
archive and journal maintenance remain separate explicit operations.

Before publication, run the required desktop and mobile browser layout
regression gate against the final integrated SHA. After deployment, verify
the live Settings toolbar, navigation, and editors remain usable at the
operator's viewport and zoom level.

## 5. Configure a managed ACP runner

Fleet can preview and apply native Codex/Goose presets or an exact ACP Registry
runner pin. ACP catalog refresh is read-only. A plan binds the distribution,
digest or exact launcher version, account reference, session options, seat,
board, private token-file reference, Fleet-owned clone, and lease/runtime
observation. Apply installs or reuses the bounded private artifact and emits a
runner template; it does not grant executor policy, sign a start request, or
start a service.

Follow [Managed runner setup](../guides/managed-runners.md). Confirm the
selected model, mode, and reasoning values were advertised by the agent's
session `configOptions`; changed or missing options must stop before the first
prompt.

Provider-authenticated ACP execution is intentionally fail-closed in this
release. The production sandbox uses a scratch home, denies operator-home and
global credential access, and has no general network permission. A provider
that requires login refresh or network access remains `needs_human` until an
operator supplies a reviewed dedicated account adapter with narrow auth roots
and egress. Do not disable the sandbox or expose an operator home directory.
ACP worker setup does not replace the independently credentialed reviewer.

## 6. Verify brand and dashboard assets

The repository and Fleet navigation use the supplied Pursers artwork from
tracked, provenance-recorded assets. Verify the public README image and the
Fleet wordmark load from the release candidate without a private path or remote
runtime dependency.

Dashboard dependencies for this cohort are MCP client 2.2.0, MCP Apps 2.0.3,
Zod 4.6.5, and Vite 8.3.2. The final candidate must pass both dashboard type
checking and a production bundle build; a lockfile-only dependency check is not
sufficient.

## Platform limitations

- macOS is the tested managed-fleet platform for this release. Linux is used
  for release wheelhouse construction, but managed seat/service acceptance on
  other operating systems is not claimed.
- Central and dashboards bind to loopback by default. Remote Central requires
  operator-supplied TLS and an explicit allowed host.
- ACP Registry selection and protocol tests do not prove provider-authenticated
  execution. That boundary requires the dedicated adapter and live acceptance
  described above.
- Fleet Dashboard browser acceptance is verifier-owned evidence. Unit fixtures
  and worker-produced screenshots are not substitutes for the required
  layout gate.
- Installing packages does not deploy, restart, migrate, or change credentials,
  live policy, service definitions, host caps, or running seats.

## Rollback

Stop new intake and scaling, preserve any new ticket, branch, cursor, and lease
evidence, then stop the 5.2.0 services without terminating a live lease holder.
Restore the prior service definitions, environments, Central instance, cursors,
executor database and policy, runner selections, delivery/index state, and
owner-only configuration as one cohort.

If startup failed before durable writes, keep the preserved data and point the
services back to the 5.1.0 environment. If Central or a configuration apply
wrote new state, use the verified backup or an operator-approved forward fix.
Do not republish different bytes under an existing version, delete tickets
created during the attempt, reset positive cursors, or copy an old
authorization fingerprint onto a new configuration revision.

After rollback, verify Central health and identity, board/archive counts,
journal continuity, saved cursor positions, live leases, registry projects,
runner locks, delivery routes, and one push wait before reopening intake.
