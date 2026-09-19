# Pursers

Pursers coordinates work, leases, and reviews on a shared board.

1. [Install uv](https://docs.astral.sh/uv/) so `uvx` is available to Zed.
2. Run `pursers-central init DIR`. It creates `DIR/worker.jwt`.
3. Set `central_url`, `board_id`, and `token_file` in the configure form. Replace the token-file placeholder with the path to `DIR/worker.jwt`.

`ca_file` is optional for a private CA. `uvx_path` selects another uvx executable. `package_spec` selects another package version or a local checkout for development.

Store only the token file path in Zed settings. Never paste the token value.
