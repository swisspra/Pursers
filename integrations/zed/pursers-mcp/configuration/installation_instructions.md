# Pursers

Pursers coordinates work, leases, and reviews on a shared board.

1. [Install uv](https://docs.astral.sh/uv/) so `uvx` is available to Zed. If uv is installed somewhere unusual, set `uvx_path` to the absolute path of its `uvx` executable.
2. Run `pursers-central init DIR`. It creates `DIR/worker.jwt`.
3. For a first-time setup, set `central_url`, `board_id`, and `token_file` in the configure form. Replace the token-file placeholder with the path to `DIR/worker.jwt`.

`ca_file` is optional for a private CA. `uvx_path` overrides automatic lookup and must be an absolute executable path. `package_spec` selects another package version or a local checkout for development.

For an existing setup, edit `context_servers.pursers` in `settings.json` directly. The Configure dialog can show `default_settings.json` instead of the saved values, and Save replaces the existing entry verbatim. Do not save placeholder values; any omitted `uvx_path` is also removed. Uninstalling the extension clears `context_servers`; after reinstalling, restore the Pursers settings before starting the server.

Store only the token file path in Zed settings. Never paste the token value.
