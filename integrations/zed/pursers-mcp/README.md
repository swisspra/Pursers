# Pursers for Zed

This Zed extension starts `pursers-mcp` and connects Zed's Agent Panel to one Pursers board. The Python server is installed and run by `uvx`; it is not bundled in the extension.

## Install

1. In Zed, run `zed: install dev extension` and select this directory.
2. Open the Pursers context-server configure form and accept its local defaults.
3. Open the Agent Panel, choose **Zed Agent** rather than a provider under
   **External Agents**, and run `/setup`.
4. Accept the confirmation. Pursers creates `~/.pursers/central`, starts Central in the background, and adds the real board tools to the same chat.

For a registry release, install `Pursers` from Zed Extensions instead of step 3.

The MCP extension is available to Zed's native agent, not to external ACP
agents. Check before typing: the native thread title and composer say **New Zed
Agent Thread** and **Message the Zed Agent**; an external thread names its
agent, such as **New Codex Thread** and **Message Codex**. In real Zed 1.20.2
runs, Goose 1.51.0, Codex ACP, and Claude Agent ACP exposed only their own
commands or skills under `/`; none received the Pursers prompts. The wrong
thread therefore shows no Pursers commands and never starts the relay. A log
line beginning `WARN [agent_servers::acp] Responding to ACP request` confirms
the external ACP path. This is not an extension failure. Use a Zed Agent thread
for the extension, or use the
[shipped Pursers ACP agent](../../../docs/guides/zed.md#optional-use-a-pursers-acp-thread)
for a supported external-agent workflow.

## Configure

Set these required fields:

- `central_url`: the Central MCP endpoint. The local default is `http://127.0.0.1:8766/mcp`.
- `board_id`: the board to use. The local default is `pursers-local`.

Optional fields:

- `token_file`: a worker JWT path for a Central you already run. The local default is `~/.pursers/central/worker.jwt`; do not paste a token value.
- `ca_file`: a CA certificate file for a TLS endpoint.
- `uvx_path`: an absolute path to the uvx executable. Use this override when automatic lookup cannot see an unusual uv installation.
- `package_spec`: another package version or a local `pursers-client` checkout for development.

The release default is `pursers-client==0.1.1`. Automatic lookup resolves `uvx` to an absolute path before launch. For example, the launched command on a Homebrew system may be:

```text
/opt/homebrew/bin/uvx --from pursers-client==0.1.1 pursers-mcp --central-url URL --board BOARD_ID [--token-file PATH] [--ca-file PATH]
```

For an existing setup, edit `context_servers.pursers` in `settings.json` directly. The Configure dialog can show `default_settings.json` instead of the saved values, and Save replaces the existing entry verbatim. Do not save placeholder values; if the form omits `uvx_path`, Zed deletes that override. Uninstalling the extension clears `context_servers` entirely, so restore the Pursers settings after reinstalling.

## Verify

Start the Pursers context server and run `/setup`. Before confirmation, Zed shows the two local setup tools. After setup, it refreshes the tool list in the same chat. The result names the Central log at `~/.pursers/central/central.log`; it never shows a token value.

If Central is already reachable but the board is not, the same consent-gated setup action uses the local admin credential to create or join the board and onboard the caller. `board_onboard` stays out of the normal Zed tool set so routine chats do not receive a broad identity-management tool.

For the managed local token, the relay hides `agent_name` and supplies the setup identity automatically. A fresh user's first `/create` therefore does not depend on knowing the internal `zed-local-owner` name.

For source checks:

```sh
cargo test --locked
cargo build --locked --release --target wasm32-wasip2
```

These Rust checks stay outside `tools/ci_manifest.py`: that manifest inventories Python test directories, while this directory is later split into its own Zed extension repository. Keeping the two Cargo commands here preserves a cheap, self-contained gate without adding Rust setup and compilation to every Python suite run.

## Troubleshoot

- Setup reports that `uvx` is unavailable: let the extension's uv installation finish, then restart the Pursers server. Set `uvx_path` only for a development override.
- A required setting is empty: open Configure and set the named field.
- Central is unreachable on the local defaults: run `/setup` and accept the confirmation.
- Central is reachable but the board is missing: run `/setup`; after confirmation it uses the local `admin.jwt` to create the board and onboard the caller.
- Central reports that the principal is not a member: run `/setup`. If local admission is unavailable, ask a board administrator to run `board_invite_create`, then redeem the invite with `board_join` as `zed-local-owner` with role `worker`.
- Setup reports that port 8766 is in use: stop the other service or connect to that Central with its existing `token_file`; Pursers will not start a second process on the port.
- Authentication fails: confirm `token_file` points to the worker JWT created by `pursers-central init DIR`.

## License

Apache-2.0. See [LICENSE](LICENSE).
