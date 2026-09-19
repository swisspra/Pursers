# Isolated Zed E2E harness

`e2e_isolated.py` verifies the Pursers MCP integration without touching the
operator's Zed profile, installed extensions, or live Central. It creates all
mutable state under one scratch root, records every child process it starts,
and tears those processes down by recorded process group.

Run it from the repository root after the Zed extension and `pursers-mcp`
server sources are present:

```sh
mkdir -p "$HOME/.cache/pursers-zed-e2e/tmp" "$HOME/.cache/pursers-zed-e2e/uv"
TMPDIR="$HOME/.cache/pursers-zed-e2e/tmp" \
UV_CACHE_DIR="$HOME/.cache/pursers-zed-e2e/uv" \
python3 tools/zed/e2e_isolated.py --timeout 240
```

Use `--extension-dir` and `--client-dir` for combined integration checkouts.
Use `--keep` only when the scratch directory is needed for debugging; the
harness still terminates its recorded Central and Zed process groups. Output
is credential-redacted and reports whether `board_status` ran through Zed or
through the explicit direct-stdio fallback.
