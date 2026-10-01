# 01 — Lean ticket reads

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 1 and shared invariants.
**Repository:** Pursers. No deployment, main merge, version bump, or manual generated-artifact changes.

## Files and ownership

- Modify `packages/central/src/pursers_central/central.py`: ticket list/get registration and projection seams.
- Add a focused pagination/read helper under `packages/central/src/pursers_central/`.
- Extend `packages/central/tests/test_ticket_archive.py`; add dedicated cursor/history tests.
- Inspect client tool registrations in `packages/client/src/pursers_client/mcp_proxy.py`; coordinate registration with stream 03.
- Update list/detail consumers under `tools/fleet-dashboard/ui/` and the applicable reference/guide. Own pagination controls, not stream 02's progress renderer.

## Steps

- [ ] Confirm current main behavior and snapshot/catch-up guarantees; annotate the exact cursor/history response schema and shared file seams before editing.
- [ ] Add failing contract tests for deterministic quiescent traversal, hot/archive boundaries, filter binding, principal mismatch, permission revocation, invalid/end cursors, and equal-priority tie breaks.
- [ ] Implement additive live-consistency keyset pagination with opaque integrity-protected cursor and bounded response bytes. Keep legacy keys/defaults and explicit detail reads.
- [ ] Add retained-history paging tests and implementation, including unavailable retention ranges and oversized individual details. Never silently delete evidence.
- [ ] Add list UI/client coverage: next page, changed filter resets cursor, deduplication, loading/error/end state, old-server fallback, and lazy detail/history. Preserve unrelated dashboard progress behavior.
- [ ] Benchmark identical synthetic 100/1,000-ticket fixtures with long histories; report bytes and archive hydration before/after. Explain remaining complexity; do not claim token savings from byte counts alone.
- [ ] Run focused Central archive/read suites, affected client/dashboard checks, leak scan, and diff check. Independent reviewer reproduces complete traversal and permission tests.
- [ ] Update docs/config/changelog; submit branch, SHA, literal test output, benchmark evidence, API examples, and compatibility notes. List generated outputs needed by the integration operator.

## Completion gate

Every matching ticket appears exactly once on an unchanged board, concurrent changes are documented honestly, page size is bounded, and snapshots retain their existing journal splice contract. No complete-board/negative-work conclusions are derived from a truncated list.
