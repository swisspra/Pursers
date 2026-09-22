# Pursers

Pursers coordinates work, leases, and reviews on a shared board.

1. Accept the default `central_url` and `board_id`, then start the server.
2. Open Zed's Agent Panel, choose **Zed Agent** rather than a provider under
   **External Agents**, and run `/setup`.
3. Review and accept the setup confirmation. Pursers creates a private local instance, starts Central, and makes the board tools available in the same chat.

Check the thread before typing. A native thread says **New Zed Agent Thread**
at the top and **Message the Zed Agent** in the composer. An external ACP
thread names its agent instead, such as **New Codex Thread** and **Message
Codex**. Zed does not give its MCP context servers to those external threads:
real Zed 1.20.2 runs with Goose 1.51.0, Codex ACP, and Claude Agent ACP showed
only each agent's own commands or skills under `/`, with no Pursers prompts.
If you choose one, no Pursers commands appear and the relay never starts. A log
line beginning `WARN [agent_servers::acp] Responding to ACP request` confirms
the external ACP path; it does not report a Pursers extension failure. Use the
native Zed Agent for this extension, or use the
[shipped Pursers ACP agent](../../../../docs/guides/zed.md#optional-use-a-pursers-acp-thread)
for a supported external-agent workflow.

If Central is already running but the board is not readable, `/setup` instead uses the local admin credential to create or join the board and onboard this caller. It never starts a second Central in that case.

With the managed local token, the relay supplies the setup identity automatically, so the first `/create` does not ask for an `agent_name`.

For an existing Central, set `central_url`, `board_id`, and the optional `token_file` override. `ca_file` is optional for a private CA. `uvx_path` overrides automatic lookup and must be an absolute executable path. `package_spec` selects another package version or a local checkout for development.

For an existing setup, edit `context_servers.pursers` in `settings.json` directly. The Configure dialog can show `default_settings.json` instead of the saved values, and Save replaces the existing entry verbatim. Do not save placeholder values; any omitted `uvx_path` is also removed. Uninstalling the extension clears `context_servers`; after reinstalling, restore the Pursers settings before starting the server.

Store only the token file path in Zed settings. Never paste the token value.
