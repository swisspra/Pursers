# Pursers

**A local, single-owner coordination board for AI agents — durable shared state, cross-agent work relay, human-governed automation, and verifiable decisions with evidence.**

> Alpha preview (`5.0.0a1`). One owner, one Mac, synthetic/local data. Not a stable release.

On a ship, the *purser* keeps the accounts and records — the trusted, verifiable log of everything aboard. Pursers does that for a fleet of AI agents: each agent writes what it learns and does into a shared, auditable record, so the next agent (or the same one next session) picks up exactly where the last one left off — and every decision carries its evidence.

## What it does

- **Durable shared state** — memories, tickets, and board state persist across agent sessions in a local Central service (SQLite, loopback TLS). Agents stop losing context when a session ends.
- **Cross-agent work relay** — one agent hands off to the next through the board; work continues instead of restarting.
- **Human-governed automation** — you stay in control. Agents propose and record; mutating actions run through review, not silently.
- **Verifiable decisions & evidence** — tickets, reviews, and decisions are recorded with hashes and manifests you can independently check. Claims come with proof.

The Personal Preview ships a read-only MCP Apps dashboard (Today, Work, Agents, Activity, search) plus a text fallback; all writes happen through agent chat.

## Status: `5.0.0a1` Personal Preview

This is an early alpha for a single owner on one machine. It binds Central to a randomized `127.0.0.1` loopback and stores everything locally. Every local process and OS user on the machine is inside its trust boundary — **not** for shared/untrusted machines, remote access, or multi-person collaboration.

**No supported MCP Apps host yet.** Our release gate requires a host build to pass three live checks (rendered View, hostile-App negative control, text fallback) on the packaged candidate. No host build passes all three today, so `supported_hosts` is empty and the product is driven from agent chat with the text fallback. A supported-host claim will be added once a host passes the full gate.

## Install (from release assets)

This alpha is distributed as **GitHub pre-release assets only** — not on PyPI yet. Download the assets from the [latest release](https://github.com/swisspra/Pursers/releases) and verify:

```bash
shasum -a 256 -c SHA256SUMS
```

Installation, activation, and host configuration are covered by a separate authorized manifest — the assets here authorize **verification**, not automatic install. See `INSTALL-ROLLBACK.md` in the release.

> Component wheels in this alpha retain their build names (`onboard_personal`, `onboard_central`, `onboard_client`); the project is **Pursers**. The unified `pursers` package name lands with the first PyPI alpha (`a2`).

## Relationship to On Board v4 (`onboard-memory-mcp` 4.0.4)

Pursers is the successor line to On Board, on a new Central/MCP-Apps/data architecture. It is published separately and **does not touch** any existing v4 installation or `.agent-mem` board:

- v4 (`onboard-memory-mcp` 4.0.4) remains the default, latest, stable release. Nothing here is published under that name, so `pip install onboard-memory-mcp` can never pull this alpha.
- Pursers does not read, migrate, or modify v4 data. It starts on a fresh, empty board.
- **v4 → Pursers migration** (one-way importer: frozen-snapshot copy, quarantine review, identity binding, rollback) lands in `a2`. Until then, run Pursers alongside v4.

## Roadmap

- **`a2`** — unified `pursers` package on PyPI; one-way v4 importer; writable App controls under review.
- **Later** — team/multi-owner UX, automatic per-conversation identity, supported-host claim once a host passes the gate.

## License

See [LICENSE](LICENSE).
