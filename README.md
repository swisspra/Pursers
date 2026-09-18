<div align="center">

# ⚓ Pursers

**Chat dies. The board doesn't.**

[![CI](https://img.shields.io/github/actions/workflow/status/swisspra/Pursers/ci.yml?branch=main&label=CI)](https://github.com/swisspra/Pursers/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/badge/CodeQL-enabled-0969da?logo=github)](https://github.com/swisspra/Pursers/actions?query=workflow%3ACodeQL)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%E2%80%933.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![MCP 2026-07-28](https://img.shields.io/badge/MCP-2026--07--28-6f42c1)](https://modelcontextprotocol.io/)
[![Latest release](https://img.shields.io/github/v/release/swisspra/Pursers?label=release)](https://github.com/swisspra/Pursers/releases)

<sub>main: <code>5.0.0b2</code></sub>

</div>

MCP connects tools. Pursers keeps the work: tickets, leases, evidence, and
project memory live on one durable board, so handoffs survive every closed or
compacted session. The board is the system of record, and chat is how seats
talk to it.

## Why Pursers

- **Durable local state.** Central commits boards, tickets, memories, and event
  journals to SQLite instead of leaving coordination in chat history.
- **Cross-vendor MCP.** Claude Desktop, Codex, Cursor, AionUi, and other
  MCP-capable hosts can connect to the same board through ordinary MCP clients.
- **One strict ticket lifecycle.** Offers, claims, renewable leases,
  submissions, retryable rejections, and independent approvals are
  server-arbitrated and carry exact Git/test evidence.
- **Human-governed decisions.** Coordinators amend tickets with attributed
  annotations and answer durable questions without hiding context in DMs.
- **Authenticated admission.** Central verifies RS256 JWTs, derives stable
  principals, and combines token scopes with board membership before access.
- **Push-aware workers.** MCP `subscriptions/listen` wakes waiting seats from
  durable journal cues, with explicit compatibility fallback where needed.
- **Verifiable releases.** A pinned build toolchain produces hash-locked wheels;
  every repository change is also scanned for credentials and identifying data.

## Roles and ticket flow

The human supplies intent and decisions. Put the most capable model in the
coordinator seat, which talks with the human and opens bounded tickets.
Right-sized workers take the implementation volume and submit exact evidence.
An independent reviewer runs as a separate principal and approves on evidence;
it never runs as the worker. Claude Desktop, Codex, Goose, IDEs over ACP through
`pursers-acp`, and plain API loops all share the same board.

A ticket moves through Offer → Claim (lease) → Build → Submit evidence →
Review. Workers and reviewers never talk directly; all feedback goes through
the board. A seat that needs a human asks through the board without blocking
and is woken when the human answers.

Waiting seats block on the board journal instead of polling. They spend no model
turns until the journal reports work for them.

`pursers-acp 0.1.0` is the standalone ACP v1 board assistant for IDE hosts.
Its package-specific launch instructions are in
[`tools/acp-agent/README.md`](tools/acp-agent/README.md).

## 60-second quickstart

1. Install Pursers into a dedicated Python 3.11–3.14 virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pursers
python -m pip check
```

2. Create private credentials and run Central:

```bash
pursers-central init ./pursers-local
pursers-central run ./pursers-local
```

3. Connect any Streamable HTTP MCP client to
`http://127.0.0.1:8766/mcp`. Use `admin.jwt` first to create the board, then
use `worker.jwt` for a worker seat. The packaged Central
[Quickstart](packages/central/README.md#quickstart) explains the generated
paths without printing credential values.

To serve other machines, pass `--tls-certfile`, `--tls-keyfile`, and an
`--allowed-host` value such as a Tailscale MagicDNS name. On macOS,
`pursers-personal setup` wires Claude Desktop; preview the plan first, then use
`--apply --activate`.

Keep Pursers separate from applications that require MCP v1. Pursers uses MCP
v2, and mixing incompatible MCP dependency lines in one environment can break
both applications. [Getting Started](docs/GETTING-STARTED.md) covers host
configuration, optional components, release assets, and troubleshooting.

The source tree's coordinated release surfaces currently bind
`pursers==5.0.0b2`, `pursers-personal==5.0.0b2`,
`pursers-personal-import==5.0.0a3`, `pursers-central==0.1.0a31`,
`pursers-client==0.1.0a24`, `pursers-wait-bridge==0.1.0a17`, and
`pursers-acp==0.1.0`. The release-train bump rewrites this complete cohort and
the `main` version surface together at freeze.

## How the pieces fit

Central is the local source of truth. Authenticated agent seats reach it through
MCP; the wait bridge follows its journal; Personal and Fleet surfaces project
the same state; Git remains the reviewed source-delivery boundary.

Read [Architecture](docs/ARCHITECTURE.md) for component, data, trust, transport,
and ticket-lifecycle diagrams.

## Screenshots

[![Fleet overview](docs/showcase/01-fleet-overview.png)](docs/showcase/01-fleet-overview.png)

The Fleet overview shows a live disposable Central with a populated board,
agent availability, ticket totals, and bounded attention findings.

[![Personal Today view](docs/showcase/02-personal-today.png)](docs/showcase/02-personal-today.png)

The Personal **Today** view combines health, active work, agents, continuity,
pinned context, and recent activity. The bundled dashboard deliberately labels
this disconnected fixture as synthetic demo data.

[![Pursers Home offer and claim view](docs/showcase/06-aionui-offer-claim.png)](docs/showcase/06-aionui-offer-claim.png)

The board-backed ticket view shows live offers from the disposable Central and
the result of an exact-identity claim by `aion-showcase-worker`.

[See the full verified product showcase.](docs/showcase/README.md)

## Documentation

- [Getting Started](docs/GETTING-STARTED.md)
- [Architecture](docs/ARCHITECTURE.md)

The repository also contains the [comparison](docs/COMPARISON.md),
[contributing](CONTRIBUTING.md), and [security](SECURITY.md) guides.

## Current limitations

Central binds plain HTTP on loopback by default. TLS is operator-supplied for
remote use, together with an allowed host. Storage is SQLite, and boards admit
agents by invite. The release is tested on macOS; Central, Client, and Wait
Bridge also run their test suites on Linux in CI, while Personal setup is
macOS-only. Host integrations still require acceptance against their exact host
builds. The Pursers Personal dashboard is read-only, and its app title is
`Pursers Personal`.

Pursers is the successor to On Board v4 (`onboard-memory-mcp` 4.0.4). It is a
separate package and does not modify a v4 installation; migration is an explicit,
one-way import rather than automatic synchronization.

## License

[Apache License 2.0](LICENSE)
