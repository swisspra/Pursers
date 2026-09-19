# Zed 1.20.2 extension API, build, and isolated dev install

Research date: 2026-09-19. The installed application was
`/Applications/Zed.app`, and its CLI reported `Zed 1.20.2`. The matching
upstream tag is [`v1.20.2` at
`7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f`](https://github.com/zed-industries/zed/tree/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f),
committed 2026-09-17. All source claims below use that commit rather than
`main`. The live Zed documentation was also checked on 2026-09-19.

## Facts we depend on

- Stable Zed 1.20.2 accepts extension API versions `0.0.1` through `0.7.0`.
  `zed_extension_api = "=0.7.0"` is therefore the exact current published
  crate to use. The source tree contains an unreleased `0.8.0`, but stable and
  preview builds explicitly cap the accepted version at `0.7.0`.
- Rust extensions compile to the component-model WASI Preview 2 target
  `wasm32-wasip2`, not `wasm32-wasip1`.
- Use rustup's `cargo` and `rustc` together. Calling rustup's `cargo` while a
  Homebrew `rustc` remains earlier on `PATH` can falsely report that the
  installed WASI target is missing.
- A Zed extension is not a general UI plug-in. It can contribute the manifest
  features described below and implement the fixed `Extension` trait, but it
  cannot create arbitrary panes, buttons, editor widgets, or webviews.
- `agent_servers` is **not** a field in the 1.20.2 `ExtensionManifest`, and the
  Rust extension trait has no agent-server hook. ACP extension submissions
  were deprecated in Zed 1.5.0 in favor of the ACP Registry.
- Extension WASM receives read/write WASI access only to its own
  `extensions/work/<extension-id>` directory. Project reads, process launch,
  downloads, npm installation, and HTTP use are mediated by host APIs.
- On macOS, a truly isolated run needs both `--user-data-dir <dir>` and a
  scratch `HOME`. The former isolates the database, extension index, installed
  extensions, staging, and work directories; the latter also keeps
  `~/Library/Logs/Zed` and other home-derived paths out of the operator profile.
- A dev install compiles the selected source directory and creates an
  `extensions/installed/<id>` symlink to it. A gallery install downloads a
  gzip-compressed tar archive, unpacks through `extensions/staging`, then
  renames it into `extensions/installed`.

Primary sources: [developing extensions](https://zed.dev/docs/extensions/developing-extensions),
[manifest implementation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs),
[builder target and defaults](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_builder.rs),
and [stable API range](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/wasm_host/wit.rs).

## Version and target

The installed binary identified itself in foreground output, including the
same full commit as the upstream tag:

```text
2026-09-19T23:47:31+07:00 INFO  [zed] ========== starting zed version 1.20.2+stable.360.7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f, sha 7c451e6 ==========
```

At that commit, the extension host's stable/preview range ends at `0.7.0`;
development/nightly builds additionally accept the unpublished `0.8.0` API.
The repository's `crates/extension_api/Cargo.toml` says `0.8.0` with
`publish = false`, while crates.io reports `0.7.0` as the latest published
crate. This is why a stable-targeted extension should pin `=0.7.0`, not use the
repository's in-development crate version.

The builder declares:

```rust
const RUST_TARGET: &str = "wasm32-wasip2";
```

Its comment identifies this as WASI Preview 2 plus the WebAssembly component
model. The official development guide says the same and states that Zed will
install the target automatically when Rust came from rustup. Sources:
[builder](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_builder.rs#L27-L29),
[API crate manifest](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_api/Cargo.toml),
[host range](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/wasm_host/wit.rs#L54-L72),
and [live development guide, checked 2026-09-19](https://zed.dev/docs/extensions/developing-extensions#developing-an-extension-locally).

## `extension.toml` schema in 1.20.2

The parser is serde over `ExtensionManifest`; unknown-field rejection is not
enabled, so a typo can deserialize without becoming a working feature. Treat
the fields in the pinned struct as the schema of record.

### Identity fields

| Field | Shape | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Stable extension identifier. Published IDs are kebab-case. |
| `name` | string | yes | Human-readable name. |
| `version` | string | yes | Extension version. |
| `schema_version` | integer | yes | Use `1` for a current `extension.toml`. Version `0` is legacy migration behavior. |
| `authors` | array of strings | no | Defaults to `[]`. |
| `description` | string | no | Defaults to absent. |
| `repository` | string | no | Defaults to absent. |
| `lib` | table | no | `kind = "Rust"` and an API `version`; the builder detects `Cargo.toml` and fills the Rust kind and compiled API version. |

### Feature fields

| Field or table | Exact 1.20.2 shape |
| --- | --- |
| `themes` | Array of relative JSON paths. JSON files in `themes/` are also discovered by the builder. |
| `icon_themes` | Array of relative JSON paths. JSON files in `icon_themes/` are also discovered. |
| `languages` | Array of relative language-directory paths. Directories under `languages/` that contain `config.toml` are also discovered. |
| `[grammars.<id>]` | Required `repository` and `rev` strings; optional `path`. `commit` remains a serde alias for `rev`. |
| `[language_servers.<id>]` | `languages` array; optional `language_ids` map and `code_action_kinds` array. Singular `language` remains deprecated compatibility input. |
| `[context_servers.<id>]` | Empty registration table. Rust code supplies the MCP server command and optional configuration. |
| `[slash_commands.<id>]` | `description` string plus `requires_argument` boolean. The parser and 0.7 trait still contain this legacy shape, but current docs say extension slash commands have been removed and new submissions are not accepted. |
| `snippets` | One relative path string or an array of relative path strings. A root `snippets.json` is auto-detected when omitted. |
| `capabilities` | Array of tagged inline tables. Supported kinds are `process:exec`, `download_file`, and `npm:install`. |
| `[debug_adapters.<id>]` | Optional `schema_path`; the default is `debug_adapter_schemas/<id>.json`, but a schema is mandatory. |
| `[debug_locators.<id>]` | Empty registration table; behavior comes from trait methods. |
| `[language_model_providers.<id>]` | Required `name`; optional relative SVG `icon`. |
| `agent_servers` | **Unsupported as an extension-manifest field in 1.20.2.** The source contains an orphaned `AgentServerManifestEntry` type, but `ExtensionManifest` has no `agent_servers` member. Use the ACP Registry for external agents. |

Representative syntax, including legacy fields only so their exact status is
unambiguous:

```toml
id = "example"
name = "Example"
version = "0.1.0"
schema_version = 1
authors = ["Example Author"]
description = "Schema example"
repository = "https://example.invalid/example"

themes = ["themes/example.json"]
icon_themes = ["icon_themes/example.json"]
languages = ["languages/example"]
snippets = ["snippets/example.json"]

[grammars.example]
repository = "https://github.com/example/tree-sitter-example"
rev = "FULL_GIT_COMMIT"
path = "grammar"

[language_servers.example-lsp]
languages = ["Example"]
language_ids = { Example = "example" }

[context_servers.example-mcp]

# Legacy parser shape; do not design a new extension around this.
[slash_commands.example]
description = "Legacy example"
requires_argument = true

[debug_adapters.example]
schema_path = "debug_adapter_schemas/example.json"

[debug_locators.example]

[language_model_providers.example]
name = "Example Provider"
icon = "icons/example.svg"

capabilities = [
  { kind = "process:exec", command = "example-server", args = ["--stdio"] },
  { kind = "download_file", host = "github.com", path = ["example", "server", "**"] },
  { kind = "npm:install", package = "example-server" },
]
```

Sources: [manifest and nested entry structs](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs),
[capability enum](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/capabilities.rs),
[snippet docs](https://zed.dev/docs/extensions/snippets),
[debugger docs](https://zed.dev/docs/extensions/debugger-extensions),
[slash-command removal](https://zed.dev/docs/extensions/slash-commands),
and [agent-server deprecation](https://zed.dev/docs/extensions/agent-servers).

### `process:exec` matching rules

Execution must pass two checks: the extension manifest must declare a matching
`process:exec` capability, and the user's `granted_extension_capabilities`
setting must also allow it. `command = "*"` matches any command. In `args`,
`"*"` matches exactly one argument at that position, while `"**"` immediately
allows all remaining arguments. Without a terminal `"**"`, extra arguments
fail the match, and missing arguments fail as well.

The host-mediated `download_file` and `npm:install` calls are checked against
the user's granted capabilities. The 1.20.2 capability docs expose all three
kinds and show how to narrow hosts, URL path segments, commands, arguments, and
npm package names. Sources: [matcher implementation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/capabilities/process_exec_capability.rs),
[two-layer execution grant](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/capability_granter.rs),
and [live capability docs, checked 2026-09-19](https://zed.dev/docs/extensions/capabilities).

## Rust `Extension` trait relevant to Pursers

These signatures come from the published `zed_extension_api 0.7.0` source. A
minimal extension needs only `new`; the remaining methods have default
implementations.

```rust
fn new() -> Self
where
    Self: Sized;

fn language_server_command(
    &mut self,
    language_server_id: &LanguageServerId,
    worktree: &Worktree,
) -> Result<Command>;

fn language_server_initialization_options(
    &mut self,
    language_server_id: &LanguageServerId,
    worktree: &Worktree,
) -> Result<Option<serde_json::Value>>;

fn language_server_workspace_configuration(
    &mut self,
    language_server_id: &LanguageServerId,
    worktree: &Worktree,
) -> Result<Option<serde_json::Value>>;

fn language_server_additional_initialization_options(
    &mut self,
    language_server_id: &LanguageServerId,
    target_language_server_id: &LanguageServerId,
    worktree: &Worktree,
) -> Result<Option<serde_json::Value>>;

fn language_server_additional_workspace_configuration(
    &mut self,
    language_server_id: &LanguageServerId,
    target_language_server_id: &LanguageServerId,
    worktree: &Worktree,
) -> Result<Option<serde_json::Value>>;

fn label_for_completion(
    &self,
    language_server_id: &LanguageServerId,
    completion: Completion,
) -> Option<CodeLabel>;

fn label_for_symbol(
    &self,
    language_server_id: &LanguageServerId,
    symbol: Symbol,
) -> Option<CodeLabel>;

fn complete_slash_command_argument(
    &self,
    command: SlashCommand,
    args: Vec<String>,
) -> Result<Vec<SlashCommandArgumentCompletion>, String>;

fn run_slash_command(
    &self,
    command: SlashCommand,
    args: Vec<String>,
    worktree: Option<&Worktree>,
) -> Result<SlashCommandOutput, String>;

fn context_server_command(
    &mut self,
    context_server_id: &ContextServerId,
    project: &Project,
) -> Result<Command>;

fn context_server_configuration(
    &mut self,
    context_server_id: &ContextServerId,
    project: &Project,
) -> Result<Option<ContextServerConfiguration>>;
```

There are no agent-server methods in this trait. For Pursers, the only
extension-side integration hook in this API family is the legacy MCP context
server pair. A separate ACP agent is configured or obtained through Zed's
external-agent/ACP registry facilities, not started by an extension trait
method. Slash-command methods remain in API 0.7 for compatibility even though
the feature is removed from current extension guidance.

Source: [`zed_extension_api 0.7.0` trait](https://docs.rs/zed_extension_api/0.7.0/zed_extension_api/trait.Extension.html)
and the [crate source corresponding to Zed 1.20.2](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_api/src/extension_api.rs).

## Minimal hello-world build proof

The fixture lived only under the worker's home cache, not in this repository.
Its complete source was:

```toml
# Cargo.toml
[package]
name = "pursers-hello-zed"
version = "0.0.1"
edition = "2024"

[lib]
crate-type = ["cdylib"]

[dependencies]
zed_extension_api = "=0.7.0"
```

```toml
# extension.toml
id = "pursers-hello-zed"
name = "Pursers Hello Zed"
version = "0.0.1"
schema_version = 1
authors = ["Pursers research fixture"]
description = "Minimal isolated dev-extension load proof"
repository = "https://example.invalid/pursers-hello-zed"
```

```rust
use zed_extension_api as zed;

struct HelloExtension;

impl zed::Extension for HelloExtension {
    fn new() -> Self {
        Self
    }
}

zed::register_extension!(HelloExtension);
```

Exact commands, with all temporary files kept below the worker's home cache:

```sh
export TMPDIR=/PATH/UNDER/HOME/.cache/zed-r1/tmp
export PATH=/PATH/TO/RUSTUP/bin:$PATH
export RUSTC=/PATH/TO/RUSTUP/bin/rustc

rustup target add wasm32-wasip2
rustup target list --installed
cargo build --release --target wasm32-wasip2
```

Literal successful build tail:

```text
   Compiling wit-bindgen v0.41.0
   Compiling pursers-hello-zed v0.0.1 (/PATH/UNDER/HOME/.cache/zed-r1/hello-zed-extension)
    Finished `release` profile [optimized] target(s) in 1m 06s
```

The resulting component was 217825 bytes with SHA-256
`a70352a374c3a51ba6cdc81a5020d8f8eec1ab0f32fe324e71a2919df9b9a330`.

The PATH failure is reproducible and worth preserving. Before `RUSTC` and
`PATH` were pinned together, rustup installed the target but Cargo selected a
different compiler and failed with:

```text
error[E0463]: can't find crate for `core`
  |
  = note: the `wasm32-wasip2` target may not be installed
  = help: consider downloading the target with `rustup target add wasm32-wasip2`
```

## Isolated dev install proof

The installed CLI exposes `--user-data-dir <DIR>`, but on macOS the pinned
source still derives `Library/Logs/Zed` from `HOME`. The proof therefore used
both a scratch home and a scratch data directory:

```sh
export HOME=/PATH/UNDER/HOME/.cache/zed-r1/zed-home
export TMPDIR=/PATH/UNDER/HOME/.cache/zed-r1/tmp
export PATH=/PATH/TO/RUSTUP/bin:/usr/bin:/bin:/usr/sbin:/sbin

/Applications/Zed.app/Contents/MacOS/zed \
  --user-data-dir /PATH/UNDER/HOME/.cache/zed-r1/zed-profile \
  /PATH/UNDER/HOME/.cache/zed-r1/hello-zed-extension
```

When concurrent Zed test instances already exist, macOS can route the stock
bundle identifier to an existing process. For this proof only, an ad-hoc-signed
scratch copy of the installed app was given a unique bundle identifier; the
operator's `/Applications/Zed.app` was never changed. In that isolated copy,
the unavoidable GUI step is `zed: install dev extension`, followed by choosing
the scratch fixture directory.

The first isolated run reached that picker with the fixture selected and the
`Open` button enabled. The operator later approved that click for scratch Zed
profiles. On the resumed run, however, the host's native-capture service could
not acquire any macOS CG window (`cgWindowNotFound`), including for unrelated
running apps, so no GUI click is claimed here.

The resumed proof instead exercised the exact post-build state transition in
`install_dev_extension`: place the compiled `extension.wasm` in the source
directory, create `extensions/installed/<id>` as a symlink to that directory,
and let the running host's installed-directory watcher rebuild and reload.
This is a source-equivalent fallback, not evidence that Zed exposes a
supported non-GUI install command. The isolated host emitted these literal
lines immediately after the symlink was created:

```text
2026-09-20T01:04:25+07:00 INFO  [extension_host] rebuilt extension index in 1.55575ms
2026-09-20T01:04:25+07:00 INFO  [extension_host] extensions updated. loading 1, reloading 0, unloading 0
```

The resulting isolated index tied that load to this fixture rather than the
built-in extension that Zed installs on first launch:

```json
"pursers-hello-zed": {
  "manifest": {
    "id": "pursers-hello-zed",
    "name": "Pursers Hello Zed",
    "version": "0.0.1",
    "lib": { "kind": "Rust", "version": null }
  },
  "dev": true
}
```

The installed entry was a symlink from
`/PATH/TO/ISOLATED/ZED-DATA/extensions/installed/pursers-hello-zed` to the
scratch fixture. The `lib.version` index field remains `null` because the
fallback did not run Zed's builder; the WASM itself is the successful
API-0.7.0 component identified above, and the host reported no load error.
For a normal developer workflow, use the documented command-palette action
and picker, which runs the builder before creating the same symlink and
reload.

Sources: [CLI reference](https://zed.dev/docs/reference/cli#--user-data-dir-dir),
[path derivation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/paths/src/paths.rs),
[documented dev-install action](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/extensions/developing-extensions.md#developing-an-extension-locally),
and [installer implementation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/extension_host.rs#L1092-L1173).

## Sandbox, network, processes, files, and limits

### Filesystem

The WASI context preopens only `extensions/work/<extension-id>` with read/write
permissions, both as `.` and by its absolute path. `PWD` is set to that
directory, and attempts to change the WASI current directory are disabled by
the API registration shim. The host's write-path resolver canonicalizes the
nearest existing ancestor and rejects any result outside the extension's work
directory, including symlink escapes.

Extensions can still receive host objects such as `Worktree` and `Project`.
Those objects expose narrowly defined operations, such as reading a text file
through the host and querying the worktree environment/PATH; they do not turn
the entire host filesystem into a WASI preopen. Source: [WASI context and path
enforcement](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/wasm_host.rs#L739-L818).

### Network and downloaded tools

Raw WASI networking is not inherited. Network access is provided through Zed
host calls. API 0.7 exposes HTTP requests, GitHub-release helpers, and
`download_file`; the latter is checked against the user's
`download_file` capability grant. Download destinations are forced into the
extension work directory. The generic HTTP client is host-mediated but is not
checked by `CapabilityGranter` in this 1.20.2 source, so do not describe the
WASM as "network-free" merely because no download capability is declared.

Language servers, debug adapters, and MCP servers are expected to be located
on PATH or downloaded into extension work storage, not bundled into published
extensions. `npm:install` is likewise a host operation and is checked against
the user's package grant.

### Process execution

WASM itself does not inherit arbitrary host process access. It calls Zed's
process API, which applies the manifest and user-grant matching described
above. For least privilege, declare an exact executable and exact fixed
arguments, using `*` or trailing `**` only when the launched tool genuinely
requires variable arguments.

### Size and resource limits

No explicit extension archive byte ceiling was found in the 1.20.2 client
download/install path or in the extension-registry validation source inspected
at `21cd47e741cd80e0c1c574da0e00bec103c6e94d`. The client reads the full
downloaded gzip archive into memory and, when `Content-Length` is present,
checks only that the received byte count matches it before unpacking. This is
a finding of "no documented/client-enforced limit located," not a promise that
the service, Git host, or review process accepts files of arbitrary size.

The publication policy separately requires including only resources the
extension needs, and forbids bundling language servers and debug adapters.
The WASM host sets an epoch deadline so calls yield cooperatively, but the
inspected construction does not configure an explicit Wasmtime memory-size
limit. Sources: [gallery download path](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/extension_host.rs#L832-L925),
[WASM store construction](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_host/src/wasm_host.rs#L640-L737),
and [publishing prerequisites](https://zed.dev/docs/extensions/publishing/prerequisites).

## Consequences for Pursers

1. A Zed extension can register a Pursers MCP context server with
   `[context_servers.<id>]` and implement the two context-server trait methods,
   but Zed's own docs warn that MCP server extensions are headed toward the
   official MCP Registry.
2. A Pursers ACP agent is not an extension hook in Zed 1.20.2. It belongs in
   the ACP Registry or Zed external-agent configuration.
3. The extension itself cannot create a custom Pursers dashboard pane. Any
   richer interaction must fit Zed's existing Agent Panel, language/debugger,
   theme/icon/snippet surfaces, or open an external/local web UI.
4. If the extension launches a Pursers helper, keep the `process:exec`
   declaration narrow and make all downloaded/runtime state live below the
   extension work directory.
