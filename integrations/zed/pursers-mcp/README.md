# Pursers for Zed

This Zed extension starts `pursers-mcp` and connects Zed's Agent Panel to one Pursers board. The Python server is installed and run by `uvx`; it is not bundled in the extension.

## Install

1. Install [uv](https://docs.astral.sh/uv/) and restart Zed so `uvx` is on its `PATH`.
2. Run `pursers-central init DIR`. This creates `DIR/worker.jwt`.
3. In Zed, run `zed: install dev extension` and select this directory.
4. Open the Pursers context-server configure form.

For a registry release, install `Pursers` from Zed Extensions instead of step 3.

## Configure

Set these required fields:

- `central_url`: the Central MCP endpoint. The local default is `http://127.0.0.1:8766/mcp`.
- `board_id`: the board to use. The local default is `pursers-local`.
- `token_file`: the path to `DIR/worker.jwt`. Replace `/PATH/TO/PURSERS/worker.jwt`; do not paste a token value.

Optional fields:

- `ca_file`: a CA certificate file for a TLS endpoint.
- `uvx_path`: another uvx executable path or command name.
- `package_spec`: another package version or a local `pursers-client` checkout for development.

The release default is `pursers-client==0.1.1`. The launched command is:

```text
uvx --from pursers-client==0.1.1 pursers-mcp --central-url URL --board BOARD_ID --token-file PATH [--ca-file PATH]
```

## Verify

Open Zed's logs and start the Pursers context server. Confirm the log shows `uvx`, the pinned package, the selected Central URL, board, and token file path. It must not show a token value.

For source checks:

```sh
cargo test --locked
cargo build --locked --release --target wasm32-wasip2
```

These Rust checks stay outside `tools/ci_manifest.py`: that manifest inventories Python test directories, while this directory is later split into its own Zed extension repository. Keeping the two Cargo commands here preserves a cheap, self-contained gate without adding Rust setup and compilation to every Python suite run.

## Troubleshoot

- `Pursers could not start uvx`: install uv, restart Zed, or set `uvx_path`.
- A required setting is empty: open Configure and set the named field.
- Central is unreachable: check `central_url`, the Central process, and `ca_file` for private TLS.
- Authentication fails: confirm `token_file` points to the worker JWT created by `pursers-central init DIR`.

## License

Apache-2.0. See [LICENSE](LICENSE).
