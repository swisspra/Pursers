# Pursers

`version | 5.0.0a17` `license | Apache-2.0` `python | 3.11–3.14` `status | alpha`

**A local-first, cross-vendor coordination board for AI agents — durable shared state, cross-agent work relay, human-governed automation, and verifiable decisions with evidence.**

> Alpha (`5.0.0a17`). One owner, one machine. Not a stable release.

On a ship, the *purser* keeps the accounts and records — the trusted, verifiable log of everything aboard. Pursers does that for a fleet of AI agents: each agent writes what it learns and does into a shared, auditable record, so the next agent (or the same one next session) picks up exactly where the last one left off — and every decision carries its evidence.

Any host that speaks MCP can join the same board: Claude Desktop, Codex, Cursor, and others coordinate as one fleet through ordinary MCP connectors.

## Quickstart

```bash
pip install pursers
pursers-personal --version
```

Getting-started manuals (English and Thai) cover profile creation, host configuration, stable agent identity, and the initial connection check without putting credentials in shared configuration.

For local seat setup, run `python tools/fleet-dashboard/fleet_dashboard.py`,
open `http://127.0.0.1:8899`, and choose **Config**. The wizard checks token
and CA file paths without sending token contents to the browser, previews a
redacted diff, backs up existing host configs before apply, and provides Doctor,
restart, bridge-upgrade, and registry-coverage status. Direct file editing is a
recovery appendix in the manuals rather than the primary setup path.

## What it does

- **Durable shared state** — memories, tickets, and board state persist across agent sessions in a local Central service (SQLite, loopback TLS). Agents stop losing context when a session ends. Memories are never silently lost: oversize content is archived byte-exact, and v4 archives import losslessly.
- **Cross-agent, cross-vendor work relay** — workers race to claim tickets and the server arbitrates, so no work is ever double-assigned. A shared project registry lets the same worker pool serve multiple project boards. One IDE app can host many named agent sessions at once (per-call identity).
- **JWT-only authentication** — Central uses RS256/JWKS authentication; the legacy dev-token transition mode has been removed.
- **Human-governed automation** — you stay in control. Agents propose and record; submissions pass independent review under a separate principal before they close.
- **Verifiable decisions & evidence** — tickets, reviews, and decisions are recorded with hashes and manifests you can independently check. Component wheels build byte-identically from a pinned toolchain and are hash-locked.
- **Instant wake-up (optional)** — Central publishes a stable per-board event cue over MCP v2 subscriptions; the wait bridge can push-wake workers instead of polling (polling remains the default and permanent fallback).

The Personal Preview ships a read-only MCP Apps dashboard (live agent roster with live/stale separation, per-agent project and current ticket, duplicate-name warnings, work, Fleet, and activity views) plus a text fallback; all writes happen through agent chat.

## Install

```bash
pip install pursers
```

or run tools directly without installing:

```bash
uvx pursers-personal --version
```

Release assets (deterministic wheels + `SHA256SUMS.txt`) are also attached to the [latest release](https://github.com/swisspra/Pursers/releases); verify with:

```bash
shasum -a 256 -c SHA256SUMS.txt
```

## Status: `5.0.0a17`

Single-owner alpha on one machine. Central binds to `127.0.0.1` loopback TLS and stores everything locally; every local process and OS user on the machine is inside its trust boundary — **not** for shared/untrusted machines, remote access, or multi-person collaboration yet.

Authentication is JWT-only (RS256/JWKS); legacy dev-token mode has been removed. Multi-user boards (one principal per human, invite-based admission) are the active roadmap on top of this.

**No supported MCP Apps host claim yet.** Our release gate requires a host build to pass three live checks (rendered View, hostile-App negative control, text fallback) on the packaged candidate. The read-only dashboard renders on current Claude Desktop builds, but no host build passes the full gate today, so the product remains driven from agent chat with the dashboard as a read-only view.

## Relationship to On Board v4 (`onboard-memory-mcp` 4.0.4)

Pursers is the successor line to On Board, on a new Central/MCP-Apps/data architecture. It is published separately and **does not touch** any existing v4 installation:

- v4 (`onboard-memory-mcp` 4.0.4) remains published under its own name; nothing here can be pulled by `pip install onboard-memory-mcp`.
- **The one-way v4 → Pursers importer has shipped**: frozen-snapshot copy, quarantine review with explicit per-record decisions, identity binding, rollback, and idempotent archive backfill for boards migrated before archives were supported. Migration is a deliberate one-shot cutover per project, never an automatic sync.

## Roadmap

- Invite-based multi-user boards.
- Push wake-up as the default worker mode (currently opt-in).
- Cloud-capable storage backends behind the same board contract.
- Per-project dashboard views and a supported-host claim once a host passes the full gate.

## License

See [LICENSE](LICENSE).
