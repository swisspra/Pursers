# Zed server-extension prior art

## Scope and method

This note examines five context-server extensions that are present in the
official [`zed-industries/extensions` registry at
`21cd47e741cd80e0c1c574da0e00bec103c6e94d`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml).
Every source link below is pinned to the exact submodule commit recorded by
that registry checkout. All five declare a `[context_servers.<id>]` entry;
four of them launch a non-Rust Node package, so the sample includes more than
the requested three context servers and two non-Rust runtimes.

The repeated shape is:

1. `context_server_command` obtains or locates the server and returns a
   `Command { command, args, env }`.
2. `ContextServerSettings::for_project` reads the extension's entry under
   `context_servers` in Zed settings.
3. `context_server_configuration` returns the three strings
   `installation_instructions`, `settings_schema`, and `default_settings`.
   In these examples, the schema is generated from a Rust settings struct.

## Comparison

| Extension | Server acquisition and version policy | Launch and configuration | Platforms and notable failures |
| --- | --- | --- | --- |
| GitHub MCP | Queries the latest non-prerelease GitHub release, downloads a matching archive, and caches its executable. It follows “latest,” not a fixed version. | Runs the downloaded `github-mcp-server stdio`; a required token setting becomes `GITHUB_PERSONAL_ACCESS_TOKEN`. | Explicit Darwin/Linux/Windows and arm64/i386/x86_64 asset mapping. Reports missing assets, directory creation, download, and directory-list errors. |
| Postgres | Installs the latest `@zeddotdev/postgres-context-server` with Zed's npm API whenever the installed version differs. | Runs Zed's Node binary on `index.mjs`; required `database_url` becomes `DATABASE_URL`. | No OS/architecture branch; portability is delegated to Zed's Node/npm layer. Explicit missing-setting error; npm, Node lookup, and JSON errors propagate. |
| Memory | Installs the exact npm version `@modelcontextprotocol/server-memory@2026.1.26`. | Runs Zed's Node binary on `dist/index.js`; optional `memory_file_path` becomes `MEMORY_FILE_PATH`. | No OS/architecture branch. npm/Node errors propagate, but malformed settings are silently ignored and an unexpected server ID panics via `assert_eq!`. |
| MarkItDown | Installs `markitdown-mcp-npx`; the default is mutable `latest`, but the user may select a package version/range. | Runs Zed's Node binary on `node_modules/.bin/markitdown-mcp-npx`; no environment variables. | No OS/architecture branch. It emits `Failed to parse settings: …`; npm and Node lookup errors propagate. |
| Playwright | Installs the latest `@playwright/mcp`. | Runs Zed's Node binary on `cli.js`; settings are translated to CLI flags and the environment is empty. | No OS/architecture branch. Installation instructions require Node on `PATH`; npm/Node failures propagate, while malformed settings silently fall back to defaults. |

The examples establish three useful patterns, but also expose tradeoffs:

- Zed can manage an npm package or download a platform-specific release asset;
  an extension does not have to bundle the server into its WASM module.
- An exact package pin, as used by Memory, is reproducible. A `latest` lookup
  improves freshness but makes the same extension build launch different code
  over time.
- Putting a secret value in `settings.json` and then copying it into `env`, as
  GitHub and Postgres do, is convenient but is not appropriate for Pursers.
  Pursers should pass only the path to a private credential file.

## 1. GitHub MCP Server: downloaded native release

Registry provenance: [`extensions.toml` names version `0.1.0`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L3149-L3151),
and [`.gitmodules` points to the LoamStudios repository](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.gitmodules#L3085-L3087).
The registry pins source commit
[`b92fd51f7d727b1b9cab0e30f2d0082a7e088d24`](https://github.com/LoamStudios/zed-mcp-server-github/tree/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24).

Acquisition and platforms. The extension asks
`latest_github_release("github/github-mcp-server", …)` for a release with
assets while excluding prereleases
([lines 32–38](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L32-L38)).
It maps Zed's platform and architecture to GitHub's asset naming convention:
Darwin/Linux/Windows plus arm64/i386/x86_64, with tar.gz on Unix and zip on
Windows
([lines 40–57](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L40-L57)).
It downloads and makes the executable runnable, then removes old version
directories
([lines 65–95](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L65-L95)).

Command and secrets. `ContextServerSettings::for_project` reads a required
`github_personal_access_token`; absence produces the literal error
`missing \`github_personal_access_token\` setting`. The returned command is the
downloaded binary with `stdio`, and the value is copied to
`GITHUB_PERSONAL_ACCESS_TOKEN`
([lines 110–129](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L110-L129)).
Other explicit failures include `no asset found matching …`,
`failed to create directory …`, and `failed to download file: …`
([lines 59–83](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L59-L83)).

Configuration contract. The method returns all three fields from embedded
files plus a Schemars-generated schema
([lines 132–148](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L132-L148)).
Its user-facing strings are:

> Installation: “To use GitHub's MCP, go to your account's Developer Settings
> and create a Personal Access Token.”

> Default settings: `{ "github_personal_access_token":
> "GITHUB_PERSONAL_ACCESS_TOKEN" }`

The exact embedded instruction and default are at
[`installation_instructions.md:1`](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/configuration/installation_instructions.md#L1)
and
[`default_settings.jsonc:1–4`](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/configuration/default_settings.jsonc#L1-L4).
The generated schema requires one string property,
`github_personal_access_token`, because the Rust field is non-optional
([lines 12–15](https://github.com/LoamStudios/zed-mcp-server-github/blob/b92fd51f7d727b1b9cab0e30f2d0082a7e088d24/src/mcp_server_github.rs#L12-L15)).
The exact `settings_schema` string generated by Schemars `0.8.22` and returned
by this pinned extension is:

```json
{"$schema":"http://json-schema.org/draft-07/schema#","title":"GitHubContextServerSettings","type":"object","required":["github_personal_access_token"],"properties":{"github_personal_access_token":{"type":"string"}}}
```

## 2. Postgres Context Server: npm latest plus a secret environment variable

Registry provenance: [`extensions.toml` records `0.0.5`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L4240-L4242),
and [the registry points to `zed-extensions/postgres-context-server`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.gitmodules#L4165-L4167).
The pinned source is
[`19eb2744dd00d9ea85e1b5ff90ded164735907dd`](https://github.com/zed-extensions/postgres-context-server/tree/19eb2744dd00d9ea85e1b5ff90ded164735907dd).

Acquisition and command. The extension compares the installed npm version of
`@zeddotdev/postgres-context-server` with the registry's latest version and
installs on mismatch
([lines 29–33](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/src/postgres_model_context.rs#L29-L33)).
It obtains Zed's Node path, launches the package's `index.mjs`, and sets
`DATABASE_URL` from the required project setting
([lines 35–53](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/src/postgres_model_context.rs#L35-L53)).
There is no explicit OS/architecture code; the Zed npm and Node APIs own that
portability. Missing configuration produces
`missing \`database_url\` setting`; JSON, npm, and Node errors propagate.

Configuration contract. The three fields are assembled at
[`postgres_model_context.rs:56–72`](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/src/postgres_model_context.rs#L56-L72).
The quoted embedded values are:

> Installation: “To use the extension, you will need to point the context
> server at a Postgres database by setting the `database_url`”

> Default settings: `{ "database_url":
> "postgresql://myuser:mypassword@localhost:5432/mydatabase" }`

See the exact
[`installation_instructions.md:1`](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/configuration/installation_instructions.md#L1)
and
[`default_settings.jsonc:1–4`](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/configuration/default_settings.jsonc#L1-L4).
The schema is generated from one required string field, `database_url`
([lines 14–17](https://github.com/zed-extensions/postgres-context-server/blob/19eb2744dd00d9ea85e1b5ff90ded164735907dd/src/postgres_model_context.rs#L14-L17)).
The exact `settings_schema` string generated by Schemars `0.8.22` and returned
by this pinned extension is:

```json
{"$schema":"http://json-schema.org/draft-07/schema#","title":"PostgresContextServerSettings","type":"object","required":["database_url"],"properties":{"database_url":{"type":"string"}}}
```

## 3. Memory MCP Server: pinned npm package

Registry provenance: [`extensions.toml` records `0.1.0`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L3177-L3179),
and [the submodule URL is `robin-afro/zed-mcp-memory`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.gitmodules#L3113-L3115).
The pinned source is
[`ca22930e96baea8d926f9fe30c84febb6e65eef1`](https://github.com/robin-afro/zed-mcp-memory/tree/ca22930e96baea8d926f9fe30c84febb6e65eef1).

Acquisition and command. Unlike the two preceding examples, this extension
pins `@modelcontextprotocol/server-memory` to exact version `2026.1.26`
([lines 10–12](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L10-L12))
and installs only when the installed version differs
([lines 42–45](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L42-L45)).
It runs Zed's Node binary on `dist/index.js`. A non-empty optional path is sent
as `MEMORY_FILE_PATH`; otherwise the environment stays empty
([lines 47–71](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L47-L71)).
There is no platform branch. npm and Node failures propagate. Two weaker
failure behaviours are worth avoiding: an unexpected server ID panics, and
settings-read or deserialization failures are silently treated as no setting
([lines 36–59](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L36-L59)).

Configuration contract. It returns:

> Installation: “Set the *absolute* path to store memories (as JSONL). Leave
> empty for default.”

> Default settings: `{"memory_file_path": ""}`

The instruction is embedded from
[`installation_instructions.md:1`](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/installation_instructions.md#L1),
while the default literal, the documented field, and the generated schema are
defined at
[`lib.rs:15–22`](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L15-L22)
and returned at
[`lib.rs:74–94`](https://github.com/robin-afro/zed-mcp-memory/blob/ca22930e96baea8d926f9fe30c84febb6e65eef1/src/lib.rs#L74-L94).
The exact `settings_schema` string generated by Schemars `1.2.1` and returned
by this pinned extension is:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"MemoryContextServerSettings","type":"object","properties":{"memory_file_path":{"description":"Path to the memory file. Leave empty to use the default location\n(next to the server's index.js). Supports absolute paths on any OS.","type":"string"}},"required":["memory_file_path"]}
```

## 4. MarkItDown MCP Server: user-selectable npm version

Registry provenance: [`extensions.toml` records `0.0.1`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L3169-L3171),
and [the submodule URL identifies the source repository](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.gitmodules#L3105-L3107).
The pinned source is
[`b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3`](https://github.com/G36maid/zed-mcp-server-markitdown/tree/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3).

Acquisition and command. The default package selector is `latest`, although
`package_version` may replace it
([lines 8–17](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/src/mcp_server_markitdown.rs#L8-L17)).
The extension checks and installs that requested version, then invokes the npm
shim through Zed's Node binary with no environment variables
([lines 41–71](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/src/mcp_server_markitdown.rs#L41-L71)).
No OS/architecture branch exists. Invalid settings return the literal prefix
`Failed to parse settings: `; npm and Node errors propagate
([lines 29–39](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/src/mcp_server_markitdown.rs#L29-L39)).

Configuration contract. It returns an embedded instruction and default,
generates the schema, and rewrites the displayed default to match the user's
current `package_version`
([lines 74–110](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/src/mcp_server_markitdown.rs#L74-L110)).
The exact text is:

> Installation: “The extension automatically installs
> `markitdown-mcp-npx` when first used. `package_version`: Specify npm package
> version (default: \"latest\").”

> Default settings: `{ "package_version": "latest" }`

See
[`installation_instructions.md:1–3`](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/configuration/installation_instructions.md#L1-L3)
and
[`default_settings.jsonc:1–6`](https://github.com/G36maid/zed-mcp-server-markitdown/blob/b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3/configuration/default_settings.jsonc#L1-L6).
The generated schema exposes one optional string, `package_version`.
The exact `settings_schema` string generated by Schemars `0.8.22` and returned
by this pinned extension is:

```json
{"$schema":"http://json-schema.org/draft-07/schema#","title":"MarkItDownModelContextExtensionSettings","type":"object","properties":{"package_version":{"default":null,"type":["string","null"]}}}
```

## 5. Playwright MCP Server: npm plus settings-to-arguments mapping

Registry provenance: [`extensions.toml` records `0.0.1`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/extensions.toml#L3205-L3207),
and [the registry points to `karlomedallo/zed-playwright-mcp`](https://github.com/zed-industries/extensions/blob/21cd47e741cd80e0c1c574da0e00bec103c6e94d/.gitmodules#L3141-L3143).
The pinned source is
[`b96f3029f05f6b3336649aaf1949f9aabc727e33`](https://github.com/karlomedallo/zed-playwright-mcp/tree/b96f3029f05f6b3336649aaf1949f9aabc727e33).

Acquisition and command. It installs the latest `@playwright/mcp` when the
installed version differs
([lines 51–55](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/src/mcp_server_playwright.rs#L51-L55)).
The settings struct covers browser, headless/vision modes, device, viewport,
profile and executable paths, CDP, isolation, storage, TLS, and output
([lines 11–37](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/src/mcp_server_playwright.rs#L11-L37)).
Those values become CLI flags; the final command is Zed's Node binary plus the
package's `cli.js`, with an empty environment
([lines 64–120](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/src/mcp_server_playwright.rs#L64-L120)).

There is no explicit platform branch. The installation instructions require
Node on `PATH`
([lines 5–8](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/configuration/installation_instructions.md#L5-L8));
npm and Node failures propagate. Invalid settings, however, are swallowed by
`unwrap_or_default`, so a typo can unexpectedly launch with defaults
([lines 57–62](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/src/mcp_server_playwright.rs#L57-L62)).

Configuration contract. The method returns the embedded instructions and
defaults plus the generated schema
([lines 123–139](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/src/mcp_server_playwright.rs#L123-L139)).
The useful literal excerpts are:

> Installation: “Node.js must be installed and available in your `PATH`.”

> Default settings: all fields are commented out; the examples are
> `"browser": "chromium"`, `"headless": false`, and `"vision": false`.

The full user example and field list are pinned at
[`installation_instructions.md:10–43`](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/configuration/installation_instructions.md#L10-L43),
and the exact commented defaults are at
[`default_settings.jsonc:1–37`](https://github.com/karlomedallo/zed-playwright-mcp/blob/b96f3029f05f6b3336649aaf1949f9aabc727e33/configuration/default_settings.jsonc#L1-L37).
The exact `settings_schema` string generated by Schemars `1.2.0` and returned
by this pinned extension is:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"PlaywrightSettings","type":"object","properties":{"browser":{"description":"Browser to use: \"chromium\", \"firefox\", \"webkit\", or \"msedge\"","type":["string","null"]},"cdp_endpoint":{"description":"Chrome DevTools Protocol endpoint","type":["string","null"]},"device":{"description":"Device to emulate, e.g. \"iPhone 15\"","type":["string","null"]},"executable_path":{"description":"Path to a custom browser executable","type":["string","null"]},"headless":{"description":"Run browser in headless mode","type":["boolean","null"]},"ignore_https_errors":{"description":"Ignore HTTPS certificate errors","type":["boolean","null"]},"isolated":{"description":"Keep browser profile in memory","type":["boolean","null"]},"output_dir":{"description":"Directory for output files","type":["string","null"]},"storage_state":{"description":"Path to storage state file","type":["string","null"]},"user_data_dir":{"description":"Path to browser user data directory","type":["string","null"]},"viewport_size":{"description":"Browser viewport dimensions, e.g. \"1280x720\"","type":["string","null"]},"vision":{"description":"Enable vision mode (screenshots instead of accessibility snapshots)","type":["boolean","null"]}}}
```

## Recommendation for Pursers: a PATH-provided, version-pinned `uvx`

Use one acquisition model: require `uvx` on `PATH`, and let it create the
isolated Python environment and install an exact Pursers distribution version.
Do not invoke ambient `python` or `pip`, do not use a mutable `latest` selector,
and do not make the Zed extension download an unofficial Pursers executable.

The extension release should compile the tested package version into a Rust
constant and return a command equivalent to:

```text
uvx --python 3.12 --from pursers-wait-bridge==<PINNED_VERSION> pursers-wait-bridge
```

`pursers-wait-bridge` is the existing stdio MCP entry point. If the extension
instead targets the Personal dashboard facade, the same acquisition rule
applies to `pursers-personal==<PINNED_VERSION>` and its `pursers-personal mcp`
entry point; the selected server must remain an extension-release decision,
not a mutable user package selector. `uvx` isolates dependencies, avoids a
global pip/pipx installation, and can provision the requested Python when uv's
managed-Python downloads are allowed. Pinning makes an extension version
launch the same Python package version on every run.

### Settings and secret boundary

Expose only these project settings:

```jsonc
{
  "central_url": "http://127.0.0.1:8766/mcp",
  "board_id": "pursers",
  "token_file": "/PATH/TO/private/worker.jwt"
}
```

The generated schema should require three non-empty strings, describe
`token_file` as an absolute path to a mode-`0600` file, and mark it as a path,
not a token. The extension must not open that file or copy its contents into a
`Command`. It should pass only:

```text
ONBOARD_CENTRAL_URL=<central_url>
ONBOARD_BOARD_ID=<board_id>
ONBOARD_CENTRAL_TOKEN_FILE=<token_file>
```

The Python child owns credential-file reading. Consequently, the bearer token
never appears in `settings.json`, the WASM extension's memory, command-line
arguments, or extension-generated diagnostics. The URL is not a secret, and
the credential path is useful configuration, but errors should still avoid
echoing file contents. Settings deserialization must fail closed with the
offending field name instead of silently selecting defaults.

### Missing-tool and missing-Python behaviour

Before constructing the command, resolve `uvx` from the project environment.
If it is absent, return one actionable error rather than falling back to
`python`, `pip`, or an unpinned executable:

```text
Pursers requires uvx on PATH. Install uv, restart Zed, and retry. The extension
will run pursers-wait-bridge==<PINNED_VERSION> in an isolated environment.
```

Include the official uv installation link in
`installation_instructions`. When `uvx` is present but Python 3.12 is absent,
allow uv to obtain its managed Python. If downloads are disabled or the
machine is offline, preserve uv's stderr and append: “Install Python 3.12, or
enable uv managed-Python downloads.” Do not silently switch to an
arbitrary system Python.

This is preferable to the alternatives:

- `pipx` still requires an independently installed Python and leaves a mutable
  global tool installation.
- plain `pip` risks contaminating the user's environment and makes executable
  discovery ambiguous.
- a Pursers standalone download would require a separately built, signed, and
  updated artifact for every supported OS/architecture; no such release
  contract exists today.
- downloading uv inside the extension duplicates uv's installer/update trust
  path. Requiring one well-known launcher on `PATH` keeps that boundary
  explicit and gives users control over managed-Python downloads.

## Reproduction commands

These are the literal successful shallow-clone commands used for this review:

```sh
git clone --depth 1 https://github.com/zed-industries/extensions.git work/TK-ca246511ed15-sources/zed-extensions
git clone --depth 1 https://github.com/LoamStudios/zed-mcp-server-github.git work/TK-ca246511ed15-sources/mcp-server-github
git clone --depth 1 https://github.com/zed-extensions/postgres-context-server.git work/TK-ca246511ed15-sources/postgres-context-server
git clone --depth 1 https://github.com/robin-afro/zed-mcp-memory work/TK-ca246511ed15-sources/mcp-server-memory
git clone --depth 1 https://github.com/G36maid/zed-mcp-server-markitdown work/TK-ca246511ed15-sources/mcp-server-markitdown
git clone --depth 1 https://github.com/karlomedallo/zed-playwright-mcp.git work/TK-ca246511ed15-sources/mcp-server-playwright
```

For repositories whose default branch had advanced beyond the registry's
submodule pointer, these literal commands fetched and selected the exact
registry commit before inspection:

```sh
git -C work/TK-ca246511ed15-sources/mcp-server-github fetch --depth 1 origin b92fd51f7d727b1b9cab0e30f2d0082a7e088d24
git -C work/TK-ca246511ed15-sources/mcp-server-github checkout --detach b92fd51f7d727b1b9cab0e30f2d0082a7e088d24
git -C work/TK-ca246511ed15-sources/postgres-context-server fetch --depth 1 origin 19eb2744dd00d9ea85e1b5ff90ded164735907dd
git -C work/TK-ca246511ed15-sources/postgres-context-server checkout --detach 19eb2744dd00d9ea85e1b5ff90ded164735907dd
git -C work/TK-ca246511ed15-sources/mcp-server-markitdown fetch --depth 1 origin b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3
git -C work/TK-ca246511ed15-sources/mcp-server-markitdown checkout --detach b29c354ed81a4a0d7123d83cf85cd6c2a1d4a1a3
```

The other two shallow clones already resolved to the registry pointers:
`ca22930e96baea8d926f9fe30c84febb6e65eef1` and
`b96f3029f05f6b3336649aaf1949f9aabc727e33`.
