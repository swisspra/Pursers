# Upgrade to Pursers 5.2.1

Pursers 5.2.1 is a focused Fleet Dashboard patch. It fixes the populated
Settings Automation policy layout and prevents cold browser loads from
overflowing Python's five-connection default listen queue while the shell and
route modules are fetched concurrently. The listener permits up to 64 queued
connections and 32 active request threads, so load is bounded while a complete
dashboard resource burst fits without connection resets.

No board schema, authentication rule, or automation policy changes in this
patch. Installing packages does not deploy, restart, migrate, or modify a
running Central, dashboard, Butler, wait bridge, or seat.

## Compatible cohort

Install these versions together:

| Distribution | Version | Release treatment |
| --- | --- | --- |
| `pursers` | `5.2.1` | New dependency metadata |
| `pursers-central` | `0.1.8` | Unchanged published artifact |
| `pursers-client` | `0.1.9` | Unchanged published artifact |
| `pursers-personal` | `5.2.1` | New product patch artifact |
| `pursers-personal-import` | `5.0.0` | Unchanged published artifact |
| `pursers-wait-bridge` | `0.1.7` | Unchanged published artifact |
| `pursers-acp` | `0.1.9` | New metadata pin for Personal 5.2.1 |

PyPI artifacts are immutable. Reuse the exact published bytes for Central
0.1.8, Client 0.1.9, Personal Import 5.0.0, and Wait Bridge 0.1.7; do not
rebuild those versions. Publish new bytes only under the new `pursers`,
Personal, and ACP versions after the final tagged candidate passes release
verification.

## 1. Prepare safely

1. Record the running package versions, service definitions, active WORK
   boards, exact seat identities, live leases, and positive wait cursors.
2. Stop new intake and scaling. Let active work finish or record each ticket's
   branch, full commit SHA, lease holder, cursor, and next action.
3. Back up the Central instance, project registry, wait cursors, Butler and
   Fleet state, delivery configuration, and owner-only service configuration.
4. Keep credentials, private paths, and host identifiers out of tickets,
   screenshots, logs, and the repository.

Do not reset cursors, recreate boards, replace authorization envelopes, or
terminate a live lease holder for this UI-only patch.

## 2. Install the patch cohort

Use a new environment so rollback remains a service-definition change:

```sh
python3.12 -m venv /PATH/TO/pursers-5.2.1
/PATH/TO/pursers-5.2.1/bin/python -m pip install --upgrade pip
/PATH/TO/pursers-5.2.1/bin/python -m pip install \
  "pursers==5.2.1" \
  "pursers-wait-bridge==0.1.7" \
  "pursers-acp==0.1.9"
/PATH/TO/pursers-5.2.1/bin/python -m pip check
```

The unchanged dependencies must resolve to the published versions in the
table above. Do not substitute locally rebuilt wheels carrying those same
versions.

## 3. Verify the Settings fix

Before deployment, run the repository's Settings browser gate at the exact
candidate SHA. It must pass all 24 light/dark, Simple/Advanced, viewport, and
200% zoom cases. The zoom case reserves scrollbar width and renders a fully
configured synthetic Automation policy with ten approved templates, capacity
fields, budget fields, connector details, and all four actions.

Inspect the generated 200% screenshots in both themes. Confirm that:

- the Automation policy card aligns with the Settings content column;
- template and connector text wraps without leaving the card;
- form fields remain legible and usable;
- Save, Reconcile, Kill, and Resume remain visible and do not overlap;
- the page itself has no horizontal overflow; and
- search, scope, mode, reload, dirty-value, and focus-preservation behavior
  still passes.

The fixture is public-safe and uses generic synthetic identifiers. It does not
contain live boards, credentials, policy values, paths, or host information.

The gate also performs 12 uncached Settings reloads against the actual bounded
listener. Every required route script, `app.js`, and `fleet.css` must load on
every attempt, Settings must render, and `refreshFleet` must initialize. A
missing resource or browser request failure fails the gate instead of leaving
the shell at `Loading...`.

## 4. Restart and observe

Point the Fleet Dashboard service at the reviewed 5.2.1 checkout or artifact
using the existing operator procedure. Do not restart Central, Butler, or seat
services solely for this CSS and fixture patch unless the local deployment
bundles them into one explicitly reviewed service unit.

After restart, open Settings on the same populated Automation policy that
exposed the defect. At 200% zoom, verify the root and card widths remain equal,
all controls remain inside the page, and no action is clipped in light or dark
mode. Read-only inspection is sufficient; do not save or reconcile a live
policy merely to validate layout.

## Rollback

Stop new dashboard operations, preserve any active ticket and lease evidence,
and restore the previous Fleet Dashboard service definition or checkout.
Because this patch changes no durable schema or policy semantics, no Central
data rollback is expected. Confirm the restored dashboard revision and one
positive-cursor push wait before reopening normal operations.

Do not republish different bytes under 5.2.0 or any unchanged component
version. If a defect remains, prepare a new reviewed patch version.
