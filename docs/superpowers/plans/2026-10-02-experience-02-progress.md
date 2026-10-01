# 02 — Durable lifecycle progress

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 2 and shared invariants.
**Repository:** Pursers. Preserve existing progress feature flag; no live configuration changes.

## Files and ownership

- `packages/central/src/pursers_central/central.py`: additive projection and authorized event/update seams; new focused activity helper alongside it.
- `packages/central/tests/test_ticket_progress.py` plus lifecycle/reconnection tests.
- `packages/client/src/pursers_client/mcp_proxy.py`: capability/notification forwarding, coordinated with stream 03.
- `tools/seat-kit/`, `tools/coordinator/`, `tools/board-butler/`: inspect actual validation/review/integration/delivery emitters and modify only necessary adapters.
- `tools/fleet-dashboard/ui/views/work.js` and ticket detail/activity rendering; coordinate actor rendering with stream 05.

## Steps

- [ ] Map existing state transitions and delivery outcomes to the design's presentation stages. Publish field vocabulary, attempt/reset rules, and ownership matrix in a ticket annotation before adapters diverge.
- [ ] Add failing projection tests covering happy flow, review rejection/retry, cancellation, lease expiry, stale/unknown estimates, and configured completion at PR rather than merge/release.
- [ ] Implement additive durable activity projection and bounded evidence references using existing state/journal. Keep legacy records readable and never renew a lease through progress.
- [ ] Add authorization and duplicate-update tests; wire actual worker, reviewer, integration, and delivery facts. Use no model calls to manufacture progress.
- [ ] Implement dashboard timeline/status with actor, freshness, blocking cause and next action. Use existing snapshot/event catch-up; test reload, reconnect, out-of-order/duplicate delivery and old-server fallback.
- [ ] Where an existing long MCP operation benefits, add opt-in per-call progress with monotonic values and unknown total support. Test no-callback and callbacks after result. Do not add slow work merely to demonstrate notifications.
- [ ] Run affected Central/client/runner/Butler/coordinator and dashboard checks. Capture browser evidence for running/waiting/stale/retry/completed and reduced-motion behavior.
- [ ] Update guide/config/changelog including flag prerequisites, compatibility and rollback. Submit SHA, literal tests, event examples and browser evidence; identify generated artifacts for operator refresh.

## Completion gate

A user can follow all supported stages without logs; unknown data remains unknown. Restarting the dashboard does not erase progress. Review approval, PR creation, merge and release stay distinct. No fake percentages, ETA, or heartbeat-driven activity.
