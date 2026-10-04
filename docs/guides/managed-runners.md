# Managed native and ACP runners

Fleet Config supports two additive runner paths for a fixed seat identity:

- keep a native Codex or Goose runner and its existing account/config reference;
- select an exact ACP Registry agent version, platform, and distribution.

There is no automatic migration. A seat keeps the same `agent_name`,
`board_id`, role, worker/reviewer principal, registry board routing, token-file
reference, cursors, and work roots. Zed can act as an ACP GUI client, but a
headless Pursers seat does not require Zed.

## Dashboard flow

Open **Config → Managed runners**.

1. Select the host platform and explicitly refresh the official ACP catalog.
   Listing the cached catalog never installs or launches anything.
2. Choose one compatible exact pin. The preview includes the registry revision,
   version, distribution, argv, integrity, cache destination, account status,
   selected session options, generated template, and activation disposition.
3. Supply logical `account_ref` and mode-`0600` token/policy file references.
   Secret values never enter the browser, preset, lock, or template.
4. Confirm the expiring plan. Apply rechecks the account and seat observations,
   installs or reuses the pinned runner, writes mode-`0600` state atomically,
   and generates both the ACP runtime config and an exact
   `pursers_fleet_executor_config_v1`-compatible seat-template record. A
   preconfigured integration may hand that record to the existing signed
   executor; otherwise apply returns `ready_for_existing_executor` for the
   operator to add to approved policy and start through the signed command path.
5. If the seat is running or has an active lease, activation is `deferred`.
   Drain it first; do not replace a live process or duplicate the seat identity.

The same loopback and same-origin policy used by other Fleet configuration
mutations protects:

- `GET /api/config/runners[?target=darwin-aarch64]`
- `POST /api/config/runners/refresh`
- `POST /api/config/runners/plan`
- `POST /api/config/runners/apply`

Plan/apply uses an expiring digest and compare-against-observation contract.
A stale catalog pin, changed account result, changed lease/runtime observation,
missing launcher, unavailable account, or unsupported option fails closed.
Generating a candidate does not authorize a template, sign an executor request,
start a service, or change an executor policy.

An ACP plan request has this secret-free shape. Paths point to private local
files or directories; the token value is never included:

```json
{
  "preset": {
    "schema": "pursers_runner_preset_v1",
    "seat": {
      "agent_name": "worker-13",
      "board_id": "pursers",
      "role": "worker"
    },
    "runner": {
      "kind": "acp",
      "account_ref": "provider:dedicated",
      "catalog_pin": {
        "agent_id": "example-agent",
        "agent_version": "1.2.3",
        "platform": "darwin-aarch64",
        "registry_revision": "sha256:...",
        "distribution_kind": "binary"
      }
    },
    "session_options": {
      "model": "example-model",
      "mode": "plan",
      "reasoning": "high"
    }
  },
  "runtime": {
    "central_url": "https://central.example.invalid/mcp",
    "token_file": "/PATH/TO/private/worker-13.jwt",
    "expected_agent_id": "AI-...",
    "expected_principal_id": "PR-...",
    "credential_ref": "worker-13-token",
    "repository": "/PATH/TO/fleet-clone",
    "work_root": "/PATH/TO/ticket-work",
    "seat_root": "/PATH/TO/seat-state"
  }
}
```

For a native preset, select `native` with provider `codex` or `goose`, preserve
its logical account/config references (and Codex profile), and send an empty
`runtime` object. No ACP catalog or install is used.

## Installation policy

Binary distributions require an upstream SHA-256 digest. Downloads are bounded
to 256 MiB and expanded content to 1 GiB/20,000 entries. Zip and tar extraction
rejects absolute paths, `..`, backslash paths, symlinks, hard links, devices,
and other special members. The verified tree is atomically renamed into a
content-addressed private cache; repeat apply reuses it.

Pinned `npx` and `uvx` entries remain argv arrays and never pass through a
shell. Fleet reports that the installed launcher owns download bounds and that
network may be used at launch. Merely viewing the catalog does not invoke the
launcher.

## Session model, mode, and reasoning

ACP choices come from the agent's session `configOptions` (or documented legacy
mode surface), not from Registry metadata or Zed settings. Save only IDs and
values the agent advertised. The managed runtime creates the session, applies
the preset in advertised dependency order, and begins the first prompt only
after every requested value is confirmed. Missing or changed options stop the
turn instead of silently falling back.

## Provider authentication limitation

The production ACP runtime currently uses macOS `sandbox-exec`, a scratch
`HOME`, and no network. It deliberately keeps the board token in the parent and
denies the agent operator-home and global credential access. A real provider
that requires network or login refresh therefore returns `needs_human` until an
operator supplies a reviewed, dedicated account adapter with narrow auth roots
and egress. Do not grant the operator home or disable the sandbox globally.
The current ACP seat adapter claims work tickets only. Keep the independent
reviewer on its existing native runner until a separate ACP review adapter is
implemented and reviewed; setup rejects an ACP reviewer activation instead of
collapsing the two principals.

## Real-provider acceptance handoff

The operator, not an automated test, performs this after configuring a dedicated
account adapter:

1. Record the exact Pursers branch and full commit under test.
2. Create separate worker and reviewer principals and a disposable WORK board
   routed through the active project registry.
3. Select and preview one exact Registry pin; confirm no secret value or private
   home path appears in the plan or generated files.
4. Start one worker through the existing executor and offer a harmless bounded
   documentation ticket. Verify claim, push wait, independent lease renewal,
   checkpoint, cancellation/recovery behavior, exact branch/commit submission,
   and no idle model calls.
5. Start an independently credentialed reviewer and verify the work. Exercise
   protocol failure and cancellation once; confirm the ticket remains
   recoverable and no duplicate seat identity appears.
6. Record provider-reported usage only when explicit counters exist. ACP token
   usage is otherwise `unknown`; do not estimate savings.

The repository fake-agent and in-process-Central suites prove protocol and
board lifecycle behavior. They are not final provider-authenticated acceptance.

## Rollback

Drain and stop the ACP seat, restore the previous preset/template backup, then
restart the native runner through its existing executor. Preserve the immutable
selection lock and last-good catalog for diagnosis. Rollback never deletes
cursors, tokens, board history, worktrees, or an active lease.
