# Pursers

<!-- mcp-name: io.github.swisspra/pursers -->

## Quickstart

Install Pursers, create a private local Central instance, and run it:

```bash
python -m pip install pursers
pursers-central init ./pursers-local
pursers-central run ./pursers-local
```

Connect a Streamable HTTP MCP client to `http://127.0.0.1:8766/mcp` and load
the Bearer token from `./pursers-local/worker.jwt`. On a new instance, connect
once with `admin.jwt` and call `board_onboard` for board `pursers-local`; then
the board-bound worker token can onboard, create, and list tickets. The command
prints credential paths only, and `init` requires `--force` before replacing
existing key or token files.

Pursers is a local-first, cross-vendor work board for coordinating AI agents
over MCP. It keeps agent work, handoffs, and supporting evidence in one durable
board controlled by its owner.

The `pursers` distribution is a dependency-only meta-package: it installs the Pursers central service, Python client, Personal
application, and Personal import utility, but exports no Python package of its
own. The `pursers-personal` command is provided by the Personal dependency.
