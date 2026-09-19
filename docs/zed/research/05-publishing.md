# Publishing Pursers in Zed's official extension registry

Research date: 2026-09-19

This is a procedure and feasibility report. It does not submit, fork, publish, tag,
or modify an installed Zed extension.

## Source snapshots

All repository rules below are pinned to immutable commits:

- `zed-industries/extensions` at
  [`21cd47e741cd80e0c1c574da0e00bec103c6e94d`](https://github.com/zed-industries/extensions/tree/21cd47e741cd80e0c1c574da0e00bec103c6e94d).
- Zed documentation and current extension source at
  [`916fc2b8cb3a815cbef4a3b40e13081be72036b6`](https://github.com/zed-industries/zed/tree/916fc2b8cb3a815cbef4a3b40e13081be72036b6).
- The extension packager actually pinned by registry CI at
  [`9ee3c503a4bbbc6b4a0f8a789acca4871d773223`](https://github.com/zed-industries/zed/tree/9ee3c503a4bbbc6b4a0f8a789acca4871d773223).

The live extension gallery was also searched on 2026-09-19 because gallery search
results are service data rather than files in either repository.

## Bottom line

Use a dedicated public repository such as `swisspra/pursers-zed`, with the registry
ID `pursers-mcp` or, even more explicitly, `pursers-mcp-server`. The wrapper must
expose exactly one MCP server and must download it through the Zed API or locate it
in the user's environment. Do not try to publish `pursers-acp` through the extension
registry: new agent-server and slash-command submissions are no longer accepted and
agent servers belong in the ACP Registry. A remote Pursers MCP endpoint should be
configured through Zed's native MCP UI instead of wrapped as an extension. These
constraints come from the pinned
[publishing prerequisites](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md)
and
[MCP extension guide](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/mcp-extensions.md).

All three requested registry IDs are currently free, but availability is not a
reservation. Recheck immediately before opening the pull request.

## What is eligible

The general publishing rules are:

- Manually test the exact submodule commit as a Zed dev extension. Publish only
  functionality not already present, do not misuse the extension API, include only
  needed resources, keep all user-facing text in English, and restrict file access
  to Zed's designated environment. Source:
  [`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md).
- An MCP-only extension must contain one MCP server and nothing else, use an ID that
  identifies it as an MCP server, and must not bundle the server. It may download the
  server or find it in the user's environment through the Zed Rust Extension API.
  MCP extensions are themselves planned for deprecation in favor of the official MCP
  registry, so the server should also be published there. Source:
  [`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md).
- A context-server extension returns a command, arguments, and environment from
  `context_server_command`. Its server may be downloaded from GitHub Releases or npm.
  This mechanism is intended for binary or npm-distributed servers; remote servers
  should be added in Zed's MCP UI. Source:
  [`mcp-extensions.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/mcp-extensions.md).
- Agent-server and slash-command extensions are deprecated and new submissions are
  rejected; agent servers should use the ACP Registry. Source:
  [`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md)
  and
  [`agent-servers.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/agent-servers.md).

For Pursers, this means the publishable shape is a small Zed WASM wrapper around one
local MCP server distribution. The existing ACP agent is a separate ACP Registry
deliverable, and an already-hosted MCP URL needs no Zed extension.

## Registry entry and repository mechanics

A new extension is added by pull request to `zed-industries/extensions` as a Git
submodule at `extensions/{extension-id}`. The source repository must be public, the
submodule URL must use HTTPS, and the recorded commit must be reachable from a branch
in the source repository. Add a matching section to the registry's top-level
`extensions.toml`; `submodule` and `version` are required, and `path` selects an
extension below the source repository root. The registry version must equal the
version in that commit's `extension.toml`. Finally run `pnpm sort-extensions`.
Source:
[`publishing-guide.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/publishing-guide.md).

The registry validator adds machine-enforced details:

- The ID contains only lowercase ASCII letters, digits, and hyphens. It must not
  start with `zed-`, end with `-zed`, or contain `extension` (apart from two
  grandfathered IDs). The submodule name and path must normally be exactly
  `extensions/{id}`. Source:
  [`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js).
- The registry `version` must be strict `major.minor.patch`: no `v` prefix, leading
  zero, prerelease, build metadata, suffix, or whitespace. Updates may not decrease
  the version, and IDs may not be renamed during an update. Source:
  [`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js)
  and
  [`src/package-extensions.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/package-extensions.js).
- The display name must not start with `Zed `, end with ` Zed`, or contain the word
  `extension`; schema version, when present in packaged metadata, must be `1`.
  Source:
  [`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js).
- The registry ID must equal `id` in the extension manifest, and the registry version
  must equal the packaged manifest version. Source:
  [`src/package-extensions.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/package-extensions.js).

### Required `extension.toml` metadata

Use all seven metadata fields shown by the official guide:

```toml
id = "pursers-mcp"
name = "Pursers MCP"
version = "0.1.0"
schema_version = 1
authors = ["<AUTHOR_NAME> <AUTHOR_EMAIL>"]
description = "Connect Zed's Agent Panel to a Pursers coordination board."
repository = "https://github.com/swisspra/pursers-zed"

[context_servers.pursers]
name = "Pursers"
```

`id`, `name`, `version`, and `schema_version` are non-optional fields in the manifest
type; the pinned packager also requires `repository` when producing registry
metadata. The official template includes non-empty `authors` and `description`, so
they should be treated as submission requirements even though the older pinned CLI
models them as optional. Sources:
[`developing-extensions.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/developing-extensions.md),
[`extension_manifest.rs` at the CI-pinned `9ee3c503...`](https://github.com/zed-industries/zed/blob/9ee3c503a4bbbc6b4a0f8a789acca4871d773223/crates/extension/src/extension_manifest.rs),
and
[`extension_cli/src/main.rs` at `9ee3c503...`](https://github.com/zed-industries/zed/blob/9ee3c503a4bbbc6b4a0f8a789acca4871d773223/crates/extension_cli/src/main.rs).

## License requirements

The extension code must use one of these recognized licenses: Apache 2.0, BSD
2-Clause, BSD 3-Clause, CC BY 4.0, GPLv3, LGPLv3, MIT, Unlicense, or zlib. A filename
whose basename begins with `LICENSE` or `LICENCE`, case-insensitively, is inspected by
content. The license must be at the extension root. If registry `path` points into a
monorepo, a license only at the repository root does not work; copy or symlink an
accepted license into the extension subdirectory. Sources:
[`license-requirements.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/license-requirements.md),
[`src/lib/fs.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/fs.js),
and
[`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js).

Pursers' root `LICENSE` is Apache-2.0, so a dedicated extension repository may use
that file directly. The monorepo option must place or symlink the Apache-2.0 license
inside the selected extension directory.

## Process, downloads, and network access

Extensions compile to WebAssembly. External server processes are started by returning
a `zed::Command` from `context_server_command`; server installation should go through
the Zed API rather than bundling an executable. Sources:
[`developing-extensions.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/developing-extensions.md)
and
[`mcp-extensions.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/mcp-extensions.md).

Zed governs privileged operations with capabilities. Users may restrict or remove
`process:exec`, `download_file`, and `npm:install`; downloads can be narrowed by host
and path, and process execution by command and arguments. A wrapper that depends on
one of these operations must expect a denied-capability error and explain the minimum
needed access. Source:
[`capabilities.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/capabilities.md).

There is no general raw-network permission documented for WASM extensions. The
supported relevant surfaces are `download_file`, npm installation, and the external
server process. The publishing prerequisites also forbid reading or modifying
outside Zed's designated environment. Sources:
[`capabilities.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/capabilities.md)
and
[`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md).

## What CI checks

The main `CI` workflow runs on pull requests, merge queue entries, and pushes to
`main`, with a ten-minute package-job timeout. It installs Node 24.21.0, pnpm 11,
Rust 1.90, and `wasm32-wasip2`; downloads a `zed-extension` binary pinned to
`9ee3c503...`; builds the registry scripts; runs their tests; packages each added or
version-changed extension; verifies sorted `extensions.toml` and `.gitmodules`; and
rejects Git LFS in submodules. Source:
[`ci.yml` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.github/workflows/ci.yml).

The package step then checks:

1. registry TOML parsing; ID format; required registry `submodule`/`version`; strict
   SemVer; HTTPS submodule URL; conventional submodule location; non-decreasing
   version; and no simultaneous remove/add ID rename;
2. equality of registry and manifest IDs and versions;
3. an accepted license at the effective extension root;
4. display-name restrictions and schema version `1`; and
5. a real release build plus validation of declared grammars, languages, themes,
   snippets, and debugger schemas before creating the extension archive.

Sources:
[`src/package-extensions.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/package-extensions.js),
[`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js),
and the
[CI-pinned extension CLI at `9ee3c503...`](https://github.com/zed-industries/zed/blob/9ee3c503a4bbbc6b4a0f8a789acca4871d773223/crates/extension_cli/src/main.rs).

The `Danger` workflow also applies PR hygiene and normally fails a PR that does not
modify `extensions.toml`. Source:
[`danger.yml` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.github/workflows/danger.yml)
and
[`dangerfile.ts` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/dangerfile.ts).

### Size and collision findings

No explicit numeric archive-size ceiling or byte-count assertion exists in the
current registry workflow, JavaScript validation, or the exact pinned CLI source.
The effective size controls are qualitative—include only needed resources—and CI's
explicit ban on Git LFS. The CLI packages only manifest-declared resources into
`archive.tar.gz`. Do not claim a numeric size limit unless Zed adds one later.
Sources:
[`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md),
[`ci.yml` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.github/workflows/ci.yml),
and the
[CI-pinned extension CLI at `9ee3c503...`](https://github.com/zed-industries/zed/blob/9ee3c503a4bbbc6b4a0f8a789acca4871d773223/crates/extension_cli/src/main.rs).

An ID must be unique by policy. In practice, `extensions.toml` is parsed into a map,
the added ID is compared with the manifest ID, and update logic compares the new map
with `origin/main`. A duplicate TOML table cannot represent a second independent
extension, and attempting to reuse an existing section is treated as an update.
Source:
[`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md),
[`src/lib/extensions-toml.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/extensions-toml.js),
and
[`src/package-extensions.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/package-extensions.js).

## Review expectations and timing

Each pull request may add or update exactly one extension. A contributor may have at
most three extension PRs open and must respond to maintainer feedback within three
weeks; otherwise the PR is closed. A personal-account fork is preferred because it
allows Zed staff to push fixes to the PR. Source:
[`publishing-guide.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/publishing-guide.md).

The stated turnaround is first feedback within a few weeks for most submissions,
sometimes one or two months, with no guarantee and potentially longer waits because
of the backlog. Severe prerequisite violations may be closed without detailed
feedback. A stale PR may be resubmitted as a fresh PR. Source:
[`faq.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/faq.md).

Submitting starts review; it does not guarantee acceptance. Source:
[`README.md` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/README.md).

## Source-layout decision

### Option A: a Pursers subdirectory selected with `path`

Example registry entry:

```toml
[pursers-mcp]
submodule = "extensions/pursers-mcp"
path = "<PATH_TO_ZED_EXTENSION>"
version = "0.1.0"
```

Advantages:

- One source repository and one commit can atomically update Pursers plus its Zed
  wrapper.
- The existing Apache-2.0 project license is compatible, provided an accepted
  license file is present inside `<PATH_TO_ZED_EXTENSION>`.
- This layout is supported and used in production. `mcp-server-zeroheight` points
  to `path = "zed"` inside `zeroheight/ai-plugins`. Sources:
  [registry entry at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L3269-L3272)
  and
  [its pinned manifest at `1e98b007...`](https://github.com/zeroheight/ai-plugins/blob/1e98b0070e6f306a41e820e513cf32265b1010a8/zed/extension.toml).

Costs:

- `path` changes the directory packaged, not the repository cloned. The registry
  helper performs a depth-one submodule checkout of the whole repository. Source:
  [`src/lib/git.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/git.js).
- A fresh depth-one Pursers clone measured `64M` total, including a `26M` `.git`
  directory and a `25.19 MiB` pack, at Pursers commit
  `552a99eea8e224572001661f478c9dff6ccc77b3`. That is CI checkout overhead even
  when only a small nested extension is packaged.
- Pursers' release cadence would force the registry submodule pointer to reference a
  large product commit, while Zed wrapper releases should generally move only when
  the wrapper's own version changes.
- The nested extension needs its own `LICENSE` or a symlink to the root license.

### Option B: dedicated `swisspra/pursers-zed` repository

Example registry entry:

```toml
[pursers-mcp]
submodule = "extensions/pursers-mcp"
version = "0.1.0"
```

Advantages:

- Small, fast registry checkout and a narrowly reviewable security boundary.
- The wrapper's version and release cadence are independent of Pursers releases;
  server version selection can be explicit in wrapper code or settings.
- `extension.toml`, `Cargo.toml`, wrapper source, configuration help, and license all
  live at the repository root, matching the most common context-server shape.
- Real dedicated-wrapper examples include
  [`arch-mcp` at `742ee712...`](https://github.com/nihalxkumar/arch-mcp-zed-extension/tree/742ee7122be6d703e76c3eabe4855c565070b879),
  which locates `uvx` in the user's environment, and
  [`azure-mcp` at `d0e51d4b...`](https://github.com/hodyhq/zed-azure-mcp/tree/d0e51d4b1242b5be2df0bab58031f5531621585e),
  which installs `@azure/mcp` with Zed-managed npm and launches it with Zed-managed
  Node. Their measured pinned checkouts were approximately `196K` and `256K`.

Costs:

- It adds a repository and a small release synchronization step.
- Compatibility between wrapper and Pursers server versions must be tested and
  documented rather than inherited from one monorepo commit.

### Recommendation

Choose option B. All three inspected MCP examples use small wrapper code; two are
dedicated wrapper repositories, and the one monorepo example is only about `292K` at
its pinned depth-one checkout. Pursers is roughly two orders of magnitude larger at
`64M`, so `path` would keep the archive focused but would not keep registry CI or
review focused. The dedicated repository also avoids coupling every Pursers release
to a registry pointer update.

## Name availability and overlap check

At `zed-industries/extensions@21cd47e741cd80e0c1c574da0e00bec103c6e94d`:

- `[pursers]` is absent.
- `[pursers-board]` is absent.
- `[pursers-mcp]` is absent.
- No `pursers` spelling or obvious `task-board`, `ticket-board`, coordination-board,
  or multi-agent-coordination label appears in `extensions.toml` or `.gitmodules`.

The live gallery additionally returned no result for `pursers`, `ticket`,
`coordination`, `project management`, `task management`, or `multi-agent` on
2026-09-19. The broad MCP category contains many infrastructure-specific servers but
no obvious Pursers-equivalent ticket/lease/offer/review coordination system. This is
a best-effort registry check, not a trademark clearance or an acceptance guarantee.
Sources:
[`extensions.toml` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml),
the live
[Zed extension gallery](https://zed.dev/extensions?filter=context-servers),
and the uniqueness/overlap rules in
[`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md).

Of the three requested candidates, `pursers-mcp` best signals the eligible product
shape. `pursers-board` describes the use case but not the required MCP-server type;
`pursers` is concise but least explicit. Before implementation, also check the more
conventional `pursers-mcp-server` and `mcp-server-pursers` forms suggested by Zed's
MCP naming guidance.

## Operator submission checklist

Do not start this checklist until the wrapper has one MCP server, an accepted license,
English user-facing copy, and successful manual testing as a Zed dev extension at the
exact commit being submitted.

### 1. Freeze and verify the source commit

In the recommended dedicated repository:

```sh
git clone https://github.com/swisspra/pursers-zed.git
cd pursers-zed
git fetch origin <EXTENSION_BRANCH>
git checkout <PURSERS_ZED_COMMIT_SHA>
test "$(git rev-parse HEAD)" = "<PURSERS_ZED_COMMIT_SHA>"
git branch -r --contains "<PURSERS_ZED_COMMIT_SHA>"
test -f extension.toml
test -f LICENSE
```

Replace the placeholder consistently with the full 40-character commit SHA. The
`git branch -r --contains` output must show a public remote branch; a commit
available only through a pull-request ref is not eligible. Confirm that manifest
`id`, `version`, server declaration, repository URL, and license contents are final.

For the monorepo alternative, run the same checks against
`https://github.com/swisspra/Pursers.git`, replace the two file checks with
`<PATH_TO_ZED_EXTENSION>/extension.toml` and
`<PATH_TO_ZED_EXTENSION>/LICENSE`, and use the full Pursers commit SHA.

### 2. Test the exact source

```sh
cargo test
cargo build --release --target wasm32-wasip2
```

Install that directory with `zed: install dev extension`, start the one declared MCP
server, exercise a read operation and a permission-gated write operation, restart
Zed, and verify denied download/process capabilities fail with actionable guidance.
Manual dev-extension testing is a publishing prerequisite; the Rust commands are the
minimum source checks, not a substitute. Source:
[`prerequisites.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/prerequisites.md)
and
[`developing-extensions.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/developing-extensions.md).

### 3. Add the exact submodule commit to a personal fork

```sh
git clone https://github.com/<GITHUB_USER>/extensions.git
cd extensions
git remote add upstream https://github.com/zed-industries/extensions.git
git fetch upstream main
git checkout -b add-pursers-mcp upstream/main
git submodule init
git submodule update
git submodule add https://github.com/swisspra/pursers-zed.git extensions/pursers-mcp
git -C extensions/pursers-mcp fetch origin <EXTENSION_BRANCH>
git -C extensions/pursers-mcp checkout <PURSERS_ZED_COMMIT_SHA>
test "$(git -C extensions/pursers-mcp rev-parse HEAD)" = "<PURSERS_ZED_COMMIT_SHA>"
git -C extensions/pursers-mcp branch -r --contains "<PURSERS_ZED_COMMIT_SHA>"
git add .gitmodules extensions/pursers-mcp
```

For the monorepo alternative, change only the submodule URL to
`https://github.com/swisspra/Pursers.git` and use `<PURSERS_COMMIT_SHA>`.

### 4. Add the registry stanza

Recommended dedicated repository:

```toml
[pursers-mcp]
submodule = "extensions/pursers-mcp"
version = "<EXTENSION_VERSION>"
```

Monorepo alternative:

```toml
[pursers-mcp]
submodule = "extensions/pursers-mcp"
path = "<PATH_TO_ZED_EXTENSION>"
version = "<EXTENSION_VERSION>"
```

`<EXTENSION_VERSION>` must be the exact unprefixed `major.minor.patch` from the
selected commit's `extension.toml`.

### 5. Sort and run the registry's local checks

```sh
pnpm install --frozen-lockfile
pnpm sort-extensions
pnpm build
pnpm test
git diff --check
git diff -- .gitmodules extensions.toml
git status --short
```

Confirm the diff contains exactly one new gitlink, one `.gitmodules` section, and one
`extensions.toml` section. Confirm no unrelated extension version changed. The PR CI
will run the pinned packager and the no-LFS check. Sources:
[`ci.yml` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.github/workflows/ci.yml)
and
[`publishing-guide.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/publishing-guide.md).

### 6. Commit, push the fork branch, and open one PR

```sh
git commit -m "Add Pursers MCP extension"
git push -u origin add-pursers-mcp
```

Open a pull request from that branch to `zed-industries/extensions:main`. State the
exact source commit and version, how the server is installed or discovered, what
commands it launches, what network/download behavior it has, the dev-extension test
result, and why it does not overlap an existing extension. Keep this as the only
extension changed by the PR, monitor CI, and answer maintainer feedback within three
weeks. Source:
[`publishing-guide.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/publishing-guide.md).

### 7. Updating after acceptance

Publish the new wrapper commit on a public branch, bump `version` in its
`extension.toml`, then update both the submodule pointer and registry version in one
new one-extension PR:

```sh
git submodule update --remote extensions/pursers-mcp
git -C extensions/pursers-mcp checkout <NEW_PURSERS_ZED_COMMIT_SHA>
# Edit only the pursers-mcp version in extensions.toml.
pnpm sort-extensions
pnpm build
pnpm test
git diff --check
```

The new version must match the selected manifest and may not decrease. Source:
[`updating-and-maintenance.md` at `916fc2b8...`](https://github.com/zed-industries/zed/blob/916fc2b8cb3a815cbef4a3b40e13081be72036b6/docs/src/extensions/publishing/updating-and-maintenance.md)
and
[`src/lib/validation.js` at `21cd47e...`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/src/lib/validation.js).

## Fresh-clone evidence

The following is literal output from the fresh depth-one clone of
`zed-industries/extensions` used for this report:

```text
$ git rev-parse HEAD
21cd47e741cd80e0c1c574da0e00bec103c6e94d
$ find .github/workflows -maxdepth 1 -type f -print | sort
.github/workflows/actionlint.yml
.github/workflows/ci.yml
.github/workflows/danger.yml
.github/workflows/remove-extension.yml
$ for id in pursers pursers-board pursers-mcp; do if grep -nF "[$id]" extensions.toml; then :; else printf '%s: FREE (no exact section)\n' "$id"; fi; done
pursers: FREE (no exact section)
pursers-board: FREE (no exact section)
pursers-mcp: FREE (no exact section)
$ if grep -niE 'pursers|task[- ]board|ticket[- ]board|coordination[- ]board|multi[- ]agent[- ]coordination' extensions.toml .gitmodules; then :; else printf '%s\n' 'no matches'; fi
no matches
$ for id in arch-mcp azure-mcp browser-tools-context-server; do grep -n -A2 -F "[$id]" extensions.toml; git ls-tree HEAD "extensions/$id"; git config -f .gitmodules --get "submodule.extensions/$id.url"; done
214:[arch-mcp]
215-submodule = "extensions/arch-mcp"
216-version = "1.0.0"
160000 commit 742ee7122be6d703e76c3eabe4855c565070b879	extensions/arch-mcp
https://github.com/nihalxkumar/arch-mcp-zed-extension.git
389:[azure-mcp]
390-submodule = "extensions/azure-mcp"
391-version = "0.1.1"
160000 commit d0e51d4b1242b5be2df0bab58031f5531621585e	extensions/azure-mcp
https://github.com/hodyhq/zed-azure-mcp.git
601:[browser-tools-context-server]
602-submodule = "extensions/browser-tools-context-server"
603-version = "0.1.0"
160000 commit 4b2f9c69fe8f734553f8c1335f947115186311d2	extensions/browser-tools-context-server
https://github.com/mirageN1349/browser-tools-context-server.git
```

The three example commits were independently checked out. Their source layouts show
the two accepted server-distribution patterns needed by Pursers: locate a user tool
(`arch-mcp`) or install a package through Zed's managed runtime (`azure-mcp` and
`browser-tools-context-server`). Registry entries and exact gitlinks are visible in
[`extensions.toml`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml)
and the
[registry tree](https://github.com/zed-industries/extensions/tree/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions).
