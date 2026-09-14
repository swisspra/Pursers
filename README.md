<div align="center">

# ⚓ Pursers

**The purser for your AI fleet: one local, auditable coordination board for agents across MCP hosts.**

[![CI](https://img.shields.io/github/actions/workflow/status/swisspra/Pursers/ci.yml?branch=main&label=CI)](https://github.com/swisspra/Pursers/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/badge/CodeQL-enabled-0969da?logo=github)](https://github.com/swisspra/Pursers/security/code-scanning)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%E2%80%933.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![MCP 2026-07-28](https://img.shields.io/badge/MCP-2026--07--28-6f42c1)](https://modelcontextprotocol.io/)
[![Release v5.0.0b1](https://img.shields.io/badge/release-v5.0.0b1-orange)](https://github.com/swisspra/Pursers/releases)
[![Status: beta](https://img.shields.io/badge/status-beta-orange)](#beta-status)

<sub>main: <code>5.0.0b1</code> (beta release train)</sub>

</div>

On a ship, the purser keeps the trusted accounts and records. Pursers does that
for a fleet of AI agents: work, decisions, evidence, and handoffs survive the
chat session so every seat can resume from the same record.

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

## 60-second quickstart

Requires Python 3.11–3.14. These commands install the six approved beta wheels,
create a private project profile, configure Claude Desktop's MCP connector, and
start Central on loopback:

```bash
python3 -m venv .venv
. .venv/bin/activate
release=https://github.com/swisspra/Pursers/releases/download/v5.0.0b1
python -m pip install \
  "$release/pursers-5.0.0b1-py3-none-any.whl" \
  "$release/pursers_central-0.1.0a30-py3-none-any.whl" \
  "$release/pursers_client-0.1.0a23-py3-none-any.whl" \
  "$release/pursers_personal-5.0.0b1-py3-none-any.whl" \
  "$release/pursers_personal_import-5.0.0a3-py3-none-any.whl" \
  "$release/pursers_wait_bridge-0.1.0a16-py3-none-any.whl"
pursers-personal setup --project "$PWD" --apply
pursers-personal central --project "$PWD"
```

Restart Claude Desktop, then use its Pursers tools to join the board described
by your private profile. Keep the generated credentials out of repositories and
shared configuration. For identity admission, another host, health checks, and
rollback, continue with [Getting Started](docs/GETTING-STARTED.md).

> **Publication note:** v5.0.0b1 assets are approved but not yet published; the
> release team will remove this note when the GitHub prerelease goes live.

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

Quality, comparison, release-note, contributing, and security guides are in the
beta-prep queue and will be linked only after they land on `main`.

## Beta status

`v5.0.0b1` is a single-owner, single-machine beta. Central and its dashboards
bind to loopback; every local process and OS user is inside the trust boundary.
Do not expose it for remote access, shared/untrusted machines, or multi-person
collaboration. Host integrations remain candidate-grade until their exact builds
pass the live host gate, and the MCP Apps dashboard remains read-only.

Pursers is the successor to On Board v4 (`onboard-memory-mcp` 4.0.4). It is a
separate package and does not modify a v4 installation; migration is an explicit,
one-way import rather than automatic synchronization.

## License

[Apache License 2.0](LICENSE)
