# Untracked runtime and merge dependencies

Audit date: 2026-09-17. Repository baseline: `origin/main` at
`75b65ab5d1b4cfecf6b4bfeb77d2ef62ff5e1cd8`.

This is an inventory, not an implementation plan. It records operational code,
configuration, and state used by the running Pursers system but absent from this
repository. Private locations are represented only by `/PATH/TO/...`
placeholders. Credential, certificate, key, token, and profile filenames were
not recorded or opened.

## Repeatable method

The sweep began from the running system rather than from the supplied seed
list. That matters: it found an active Personal-dashboard launcher and the
seat-specific launch inputs that the seeds did not name.

1. Inventory processes and listeners, then reduce every command to executable,
   script, and non-sensitive path categories before recording it.
2. Enumerate loaded Pursers launch jobs. Inspect only their labels, program
   arguments, working directories, log destinations, and environment-variable
   *names*. Do not print environment values.
3. Follow each referenced launcher to the source, runtime checkout, state, and
   trust-input categories it reads at startup. Do not open private inputs.
4. Compare every source-like file with `git ls-files`. For generated runtime
   checkouts, record the Git commit and cleanliness rather than treating a
   deployed copy as new source.
5. Measure files with byte and line counts, then search the repository for a
   functional replacement. A validator, UI, or test harness counts as a
   replacement only if it performs the same operation.

Command shapes used (private paths and listener details are intentionally not
present):

```sh
/bin/ps -axo pid=,ppid=,etime=,command=
/usr/sbin/lsof -nP -iTCP -sTCP:LISTEN
launchctl list | awk 'tolower($3) ~ /pursers/'
launchctl print "gui/$(id -u)/com.pursers.fleet-dashboard"
launchctl print "gui/$(id -u)/com.pursers.coordinator"
find /PATH/TO/FLEET -maxdepth 2 -type f
find /PATH/TO/OPERATOR_CACHE -maxdepth 1 -type f
git -C /PATH/TO/REPOSITORY ls-files --error-unmatch PATH
git -C /PATH/TO/RUNTIME_SOURCE rev-parse --verify 'HEAD^{commit}'
git -C /PATH/TO/RUNTIME_SOURCE status --short
wc -l PATH
du -h PATH
```

The process pass was bounded to Pursers-related commands, but the listener pass
was not seeded with script names. The launch-job pass similarly began from
loaded labels and followed their references. This makes the method capable of
finding an unlisted dependency rather than merely confirming the seeds.

## Findings

Each row has exactly one disposition:

- **SHOULD BE PRODUCT**: maintained in `packages/` or `tools/` and delivered as
  part of the supported runtime.
- **SHOULD BE TRACKED**: reviewed operator/developer material in this repository,
  but not shipped to end users.
- **CORRECTLY LOCAL**: generated, mutable, machine-specific, or private material
  that must not be committed.

| ID | Untracked item and measured size | What depends on it; loss impact | Existing replacement | Disposition and reason |
|---|---|---|---|---|
| F01 | Fleet `launch.sh`, 4,049 bytes / 85 lines | Starts the Codex worker/reviewer roster with the required model, access mode, role prompt, working directory, and log routing. Loss stops new development seats from starting. | No tracked command reproduces this Codex fleet policy. Seat-kit targets a different lifecycle. | **SHOULD BE TRACKED** — it is operator policy and automation, not a secret or product binary. |
| F02 | Fleet `supervise.sh`, 4,067 bytes / 122 lines | Restarts one-shot seats, applies crash-loop backoff, and cleans bounded scratch directories. Loss drains the active development fleet as seats exit. | Coordinator supervision observes work but does not launch these local processes. | **SHOULD BE TRACKED** — restart and cleanup policy needs review and tests. |
| F03 | Fleet `status.sh`, 798 bytes / 22 lines | Reports local process/log state for the roster. Loss does not stop product or development, but removes the only compact local view. | Board status reports authoritative board state, not local processes or log health. | **SHOULD BE TRACKED** — small operator diagnostic with no private data requirement. |
| F04 | Current TLS Central launcher, 6,309 bytes / 167 lines | Configures and serves the live Central listener and binds it to private trust inputs and persistent state. Loss stops the product listener. | The tracked Central runtime now provides TLS and a guarded Host allowlist on `main` after `TK-152f512e1695`; the live service still uses this external launcher until operator cutover. | **SHOULD BE PRODUCT** — this requirement is now satisfied in source; retire the one-machine shim after cutover rather than copying it into the repository. |
| F05 | Integration-manifest digest regenerator, 1,665 bytes / 53 lines | Rewrites digests while asserting the path set and order are unchanged. Every merge that changes a hashed file depends on it; loss blocks the mandated merge procedure, while a bug can silently corrupt the gate. | `tools/regenerate_integration_manifest.py` is now the reviewed writer; `tools/ci_manifest.py` remains the independent validator. | **TRACKED** — the release-owned merge command is reviewed and tested, while execution remains operator-only. |
| F06 | Dashboard attention acknowledger, 1,636 bytes / 54 lines | Bulk-acknowledges selected attention records. Loss does not stop the system; it removes an operator shortcut. | The tracked dashboard can acknowledge records interactively, but there is no tracked equivalent for the bounded bulk operation. | **SHOULD BE TRACKED** — it mutates operational state and should not be an unreviewed cache script. |
| F07 | Browser201 capture script, 7,785 bytes / 176 lines | Drives a real browser and emits the operator-owned Browser201 evidence set. Loss blocks exact reproduction of that acceptance evidence. | The tracked Home acceptance harness shares primitives but does not reproduce this exact capture. | **SHOULD BE TRACKED** — acceptance evidence generation must be repeatable and reviewable. |
| F08 | Fleet redesign capture script, 4,339 bytes / 112 lines | Captures the Fleet candidate at operator-selected viewports. Loss blocks reproduction of those visual acceptance artifacts, not product runtime. | General browser-test machinery exists, but this recipe does not. | **SHOULD BE TRACKED** — it is a release/acceptance recipe, not machine-private state. |
| F09 | Model-attribution capture script, 3,713 bytes / 90 lines | Captures model/provider attribution from live Fleet cards. Loss blocks reproduction of this evidence. | Product tests cover fields; they do not recreate the live capture. | **SHOULD BE TRACKED** — same evidence-chain reason as F07-F08. |
| F10 | Loaded Fleet-dashboard launch-job definition, 1,198 bytes | Starts and restarts the dashboard from a local runtime and routes logs. Loss stops automatic dashboard recovery after logout/reboot. | The repository documents manual launch steps, but no installed, sanitized job template is authoritative. | **SHOULD BE TRACKED** — commit a placeholder-only template; keep injected paths and credentials local. |
| F11 | Loaded coordinator launch-job definition, 1,504 bytes | Starts and restarts the coordinator with its mode and intake wiring. Loss stops automated coordination after logout/reboot, while workers can continue manually. | Coordinator CLI documentation permits reconstruction, but there is no authoritative job template. | **SHOULD BE TRACKED** — lifecycle policy belongs in review; private argument values do not. |
| F12 | Active Personal-dashboard launcher, 1,945 bytes / 51 lines | Adapts the separately installed Personal dashboard to the current Central transport. Loss stops that Personal surface, not Central itself. This item was found from the process tree, not the seeds. | Tracked Personal server construction exists, but the active adapter is absent and the supported profile path is not a drop-in replacement for this deployment. | **SHOULD BE PRODUCT** — the compatibility adapter should either become a supported entry point or be removed after migration. |
| F13 | Seat roster/instruction inputs: 44 instruction files across 24 seat directories, about 151.5 KiB / 833 lines | The fleet launcher assumes these role directories and their startup contracts exist. Loss prevents faithful worker/reviewer recreation and erases local handoff context. This item was found by following F01-F02. | Some scaffolding exists, but the exact roster, access tiers, and launch contract have no canonical tracked specification. | **SHOULD BE TRACKED** — store a sanitized roster/template or generator; keep per-seat credentials, cursors, and notes local. |
| F14 | Live Central persistent database, about 87 MiB at observation time | Holds the authoritative board, ticket, journal, and identity state. Loss stops the product and can destroy history, not merely delay development. | No repository backup/restore runbook or equivalent source of truth was found. Git cannot replace mutable service data. | **CORRECTLY LOCAL** — live data must not enter Git, but it needs an encrypted backup and tested restore procedure. |
| F15 | Central trust and machine-specific configuration bundle; metadata-only enumeration found at least 225 private-input files totaling about 4.6 MiB | Supplies the listener identity, authentication trust, endpoint configuration, and private credentials. Loss stops authenticated product access even if F14 survives. Contents and filenames were not inspected. | Private inputs must be reissued or restored as one coherent unit; tracked code cannot replace them. | **CORRECTLY LOCAL** — these materials must never be committed; only their schema, backup policy, and recovery checks belong in the repository. |
| F16 | Generated coordinator and Fleet runtime checkouts/virtual environments, roughly 1.4 GiB plus an 11 MiB coordinator checkout | Provide the exact code and dependencies used by the two loaded jobs. Loss stops those jobs until rebuilt. Both source checkouts were clean and resolved to Git commits. | Rebuild from the recorded repository commits and dependency metadata. | **CORRECTLY LOCAL** — deployed artifacts are reproducible caches, not source; record provenance and rebuild instructions instead of committing them. |

The four supplied seed groups account for F01-F11: three fleet scripts, one TLS
launcher, five cache-resident operator scripts, and two launch-job definitions.
The process/startup sweep added F12-F16. Ephemeral test worktrees, ordinary logs,
and per-run browser profiles were observed but excluded because no running or
merge-time dependency pointed to their contents.

## Operator merge-gate regeneration

Run these commands only in the final merge checkout. Worker branches must leave
both generated artifacts untouched. When a merge changes anything under
`packages/`, first rebuild `component-lock.json` from an empty wheel directory.
Then regenerate the integration manifest. Run the manifest command a second
time: because `component-lock.json` is itself one of the hashed paths, the
second run must report `already current` and proves the sequence is stable.

```sh
python3 tools/regenerate_component_lock.py \
  --wheel-dir /PATH/TO/EMPTY/WHEEL-DIRECTORY
python3 tools/regenerate_integration_manifest.py
python3 tools/regenerate_integration_manifest.py
python3 tools/ci_manifest.py run
```

For a merge that does not change `packages/`, omit only the component-lock
command. The regenerator preserves the manifest's exact path set, sorts rows,
fails on malformed, duplicate, missing, or repository-escaping inputs, and
does not rewrite an already-current file.

## What fails first if the machine is lost

1. **F14, Central persistent state.** The product loses its authoritative
   history immediately. Recovery requires an encrypted, application-consistent
   database backup; restore to a private data directory; integrity and schema
   checks; confirmation of journal watermarks and board counts; then a read-only
   health check before dispatch resumes. If no independent backup exists, Git
   cannot reconstruct the lost board history.
2. **F15, Central trust and machine configuration.** A surviving database is not
   usable by existing clients without a coherent trust/configuration set.
   Recovery requires restoring or reissuing the private inputs together,
   updating clients through an authorized rotation, checking file permissions,
   and proving authenticated read/write isolation before opening service.
3. **F04, the TLS Central launcher.** With data and trust restored, recover by
   deploying the tracked Central entry point now present on `main`, injecting
   private inputs outside Git, then verifying health, authentication, Host
   validation, and rollback. Re-creating the private shim from memory would
   preserve the bus-factor risk.

F01-F02 fail next for development: already running seats eventually exit and no
reviewed mechanism recreates the roster. F10-F11 then matter at the next service
restart. F05 blocks the next affected merge rather than the current runtime.

## The first source fix

F05 was the first incorrectly untracked item to fix. The data and trust material
rank above it for outage recovery, but are correctly local by nature; F04 has a
tracked replacement and needs an operator cutover, not another source
implementation. The reviewed regenerator now covers the critical merge path and
keeps execution release-owned.

After F05, track sanitized fleet and launch-job templates, fold the three capture
recipes into the acceptance harness, and either productize or retire the active
Personal launcher. Separately, document and exercise encrypted backup/restore for
F14-F15 without ever placing private material in the repository.

## Privacy check

This report contains no real home directory, hostname, tailnet name, port-bound
URL, credential value, or private-input filename. Paths that a reader must supply
use `/PATH/TO/...` placeholders. The audit read source and launch metadata only;
it did not open credential, certificate, key, token, or profile files.
