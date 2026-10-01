# 05 — Persistent display names without identity changes

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 5 and shared invariants.
**Repository:** Pursers. Scope is board-local profile display, not authentication redesign.

## Files and ownership

- `packages/central/src/pursers_central/central.py`: existing agent identity/registration and narrow profile update seam; focused profile helper if warranted.
- `packages/client/src/pursers_client/mcp_proxy.py`: authorized API exposure, coordinated with stream 03.
- `tools/fleet-dashboard/ui/views/team.js`, `ui/assets/app.js`, and actor rendering shared with stream 02.
- Agent identity/auth/profile tests and relevant dashboard/client tests; adding-agents guide and changelog.

## Steps

- [ ] Freeze additive profile fields/update API and board scope. Confirm exact identity derivation and test that renaming the display label cannot alter it.
- [ ] Add failing tests for persistence across restart/token refresh, old records, owner/admin permissions, unauthorized edits, revision conflict, reset, audit history and cross-board isolation.
- [ ] Implement validation, persistence and authorized compare-and-set update. Preserve all role/capability/lease/JWT behavior. Reject unsafe control/bidi override text while supporting normal international names.
- [ ] Add safe UI edit/save/cancel/error flow. Duplicate labels are allowed with operational-name/board disambiguation; IDs remain selection keys. Keep original event actor evidence intact.
- [ ] Test XSS/text rendering, Unicode, identical aliases, deleted/retired identities, keyboard navigation, focus and permission feedback. Capture browser evidence for update/reset/conflict and fallback names.
- [ ] Run focused Central/client/dashboard checks, leak scan and diff check. Coordinate actor markup with stream 02 rather than duplicate its renderer.
- [ ] Document scope and persistence limits, changelog and old-client behavior; submit SHA, literal tests and browser evidence. No live agent renames during implementation.

## Completion gate

Display names survive normal restarts, while agent_id/agent_name/principal/leases and historical ownership are unchanged. No user can impersonate another identity through a label or edit someone else's profile without authority.
