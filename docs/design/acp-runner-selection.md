# ACP runner catalog and preset contract

Status: foundation contract; installation, launch wiring, dashboard controls, and
provider acceptance are downstream work.

## Source and trust boundary

Pursers uses the official ACP Registry only as a discovery source. This is
separate from the MCP Registry and from Pursers' project registry. The schema
was inspected on 2026-10-03 at upstream commit
`7bc5035519748873205d3100f096d48109ce9f3e`; the published v1 feed is
`https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json`.

Registry text, package names, archive URLs, commands, and arguments are
untrusted. `tools/acp-seat/runner_catalog.py` parses a bounded JSON document and
returns argv arrays. It never passes registry text to a shell and never
installs, downloads an agent archive, or launches an agent. A catalog list is
therefore safe to render without side effects.

An explicit refresh has a finite request timeout and a 4 MiB hard maximum. It
validates the complete response before atomically replacing the mode-0600
last-good cache. A failed, malformed, or oversized refresh leaves that cache
unchanged. Offline reads report `stale=true` after the configured age but retain
the validated catalog for an operator-visible offline view.

The cache revision is `sha256:<digest>` over the canonical validated catalog
projection (`version` plus the exact normalized agent records retained by the
resolver). Every offline load revalidates that projection and recomputes the
digest, so cache content cannot change under an old revision. This is a local
immutable revision even when the CDN response has no source commit. The
upstream schema commit above records the format used to implement the parser;
it is not substituted for a fetched feed revision.

## Resolution and selection lock

Resolution always requires `agent_id`, exact `agent_version`, platform target,
and optionally an explicit distribution kind. Values such as `latest`,
`stable`, and `preview` are rejected. Supported targets follow the official v1
registry names: Darwin, Linux, or Windows crossed with `aarch64` or `x86_64`.
An `npx` package must be an unscoped or scoped npm name whose final `@version`
equals the agent version. An `uvx` package must use either the official
`name==version` or `name@version` form with the same exact version. Unpinned,
alias, mismatched, and option-like package values fail validation.

The resolver prefers a matching binary, then `npx`, then `uvx`, unless the
caller selects a kind. A binary unavailable for the current platform is not
silently replaced when the caller explicitly selected it. Catalog rows with no
usable distribution are disabled with
`unsupported_platform_or_distribution`.

Resolution produces a non-executing `pursers_acp_resolved_runner_v1` preview:

```json
{
  "schema": "pursers_acp_resolved_runner_v1",
  "agent_id": "example-agent",
  "agent_version": "1.2.3",
  "platform": "darwin-aarch64",
  "registry_revision": "sha256:...",
  "distribution": {
    "kind": "binary",
    "source": {"archive": "https://example.invalid/agent.tgz"},
    "integrity": {"algorithm": "sha256", "digest": "..."}
  },
  "launch": {"argv": ["./agent", "acp"], "cwd": "install_root"}
}
```

`cwd: "install_root"` is a symbolic downstream binding, not a filesystem path.
Package resolutions use an argv beginning with `npx` or `uvx` and `cwd: null`.
The downstream installer must independently enforce archive/package policy,
verify integrity when present, bind `install_root`, and use an exec-style API.

Persisting that result creates `pursers_acp_runner_lock_v1`. The file is
mode-0600 and immutable: writing identical bytes is idempotent, while a version,
revision, platform, distribution, source, integrity, or argv change is pin
drift and fails closed. Updating a runner is a deliberate replacement workflow,
never a background refresh side effect.

Both lock creation and loading validate the complete resolved contract. Binary
sources require an HTTPS archive, an install-root-relative command, and either
no integrity value or a SHA-256 object with a 64-hex digest. Package sources
require the exact package pin, null integrity, the matching `npx` or `uvx`
executable, and identical package text in argv position one.

## Portable per-seat preset

`tools/acp-seat/runner_preset.py` defines `pursers_runner_preset_v1`:

```json
{
  "schema": "pursers_runner_preset_v1",
  "seat": {
    "agent_name": "worker-13",
    "board_id": "pursers",
    "role": "worker"
  },
  "runner": {
    "kind": "acp",
    "account_ref": "gemini:company",
    "catalog_pin": {
      "agent_id": "gemini-cli",
      "agent_version": "1.2.3",
      "platform": "darwin-aarch64",
      "registry_revision": "sha256:...",
      "distribution_kind": "npx"
    }
  },
  "session_options": {"model": "example-model", "mode": "plan"}
}
```

`seat` preserves the Pursers role and board identity independently of the
runner. `account_ref` and native `config_ref` are logical references, not secret
values or paths. `session_options` contains values negotiated or selected for
an ACP session; it is separate from authentication/account configuration.
Secret-like keys and absolute/private paths are rejected.

Native Codex and Goose remain supported through `runner.kind: "native"` with
`provider`, `account_ref`, `config_ref`, and `codex_profile`. A Codex preset
requires and preserves `codex_profile`; Goose requires it to be null. The
bounded legacy native shape is migrated without changing `agent_name`,
`board_id`, `role`, or `codex_profile`.

ACP `configOptions` belong to the companion session-negotiation interface. They
are agent-advertised session choices, not universal account profiles. The
catalog/preset contract deliberately stores only selected session option values
and makes no assumption that option IDs are portable between agents or
versions. It never inherits Zed settings automatically. A headless Pursers seat
does not require Zed to be running.

## Migration, compatibility, and rollback

1. Normalize an existing native preset and inspect the resulting v1 document.
2. Keep its seat identity, native account/config references, and
   `codex_profile`; no catalog access is required for native runners.
3. For ACP, explicitly refresh, choose an exact version/platform/distribution,
   review the argv preview, and create the immutable selection lock.
4. Only the downstream managed-runtime integration may install and launch the
   selected runner.

The new modules are additive and do not alter existing ACP seat configuration,
Codex/Goose runners, board identity, or leases. Roll back by stopping downstream
use of the new preset/lock and returning to the preserved native preset. Do not
delete a last-good cache or immutable lock as an automatic recovery action;
retain it for diagnosis and require an explicit operator replacement.

The current production ACP seat remains macOS-only because
`pursers_acp_seat.py` requires `sandbox-exec` and denies network access to the
agent subprocess. Selecting a registry entry does not relax that sandbox. The
parent/downstream installer and launcher design must preserve credential
separation and must not give the agent network access merely because the
catalog advertises an install source.
