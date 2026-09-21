# Pursers

Pursers coordinates work, leases, and reviews on a shared board.

1. Accept the default `central_url` and `board_id`, then start the server.
2. Open Zed's Agent Panel and run `/setup`.
3. Review and accept the setup confirmation. Pursers creates a private local instance, starts Central, and makes the board tools available in the same chat.

For an existing Central, set `central_url`, `board_id`, and the optional `token_file` override. `ca_file` is optional for a private CA. `uvx_path` overrides automatic lookup and must be an absolute executable path. `package_spec` selects another package version or a local checkout for development.

For an existing setup, edit `context_servers.pursers` in `settings.json` directly. The Configure dialog can show `default_settings.json` instead of the saved values, and Save replaces the existing entry verbatim. Do not save placeholder values; any omitted `uvx_path` is also removed. Uninstalling the extension clears `context_servers`; after reinstalling, restore the Pursers settings before starting the server.

Store only the token file path in Zed settings. Never paste the token value.
