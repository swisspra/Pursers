# 03 — Lazy MCP resources and prompts

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 3 and shared invariants.
**Repository:** Pursers. SDK 2.2.0 already present; no dependency migration required.

## Files and ownership

- `packages/client/src/pursers_client/mcp_proxy.py`: existing six prompts and role-filtered tools.
- Add a focused versioned help/catalog module rather than embedding full docs in tool descriptions.
- `packages/central/src/pursers_central/central.py`: only necessary authorized resource registration; coordinate shared edits.
- `tools/wait-bridge/pursers_wait_server.py`: existing digest resource and legacy/current transport compatibility.
- `packages/client/tests/test_mcp_proxy.py`, `test_proxy_security.py`, applicable Central and wait-bridge tests; existing client/role guides.

## Steps

- [ ] Record existing prompts/resources and transport behavior. Publish final URI/capability manifest for stream 04; preserve old digest URI compatibility.
- [ ] Add failing tests for minimal index, lazy content fetch, role/board authorization, permission changes, missing engine, and no private information in catalog/error output.
- [ ] Implement static help catalog and explicitly authorized dynamic resource adapters. Keep private caches scoped to current authority; prefer no dynamic caching without proof of safe invalidation.
- [ ] Improve existing prompts for role-aware, host-neutral next actions without executing mutations. Keep Context injected server-side; no model-visible credential/header authority.
- [ ] Exercise real resource reads/prompt rendering and subscription negotiation over current protocol and legacy Zed relay path. Verify explicit unsupported/unavailable fallbacks and no misleading empty board.
- [ ] Measure discovery and representative workflow payloads against baseline. Keep catalogs concise and do not preload the entire skill bundle.
- [ ] Run focused client/security/transport and wait-bridge checks; update docs/changelog and provide trace excerpts stripped of secrets. Independent reviewer exercises least-privileged user and unsupported host.
- [ ] Submit SHA, literal output, final URI manifest, compatibility table and generated-artifact requests.

## Completion gate

Supported hosts discover and read useful role-specific content; unsupported hosts retain a documented tool/docs path. Resources, prompts and Context confer no new authority and do not duplicate full board history.
