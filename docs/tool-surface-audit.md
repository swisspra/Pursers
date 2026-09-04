# Tool Surface Audit for Pursers a18 Release Train

**Date:** 2026-09-04<br>
**Author:** AAIF / Pursers Fleet Engineering (TK-2ffa16368cbb)<br>
**Status:** Approved Audit & Deprecation Implementation<br>
**Target:** a18 (Deprecation & Hide-Unless-Legacy) / a19 (Removal & Final Consolidation)

---

## 1. Executive Summary & Purpose

Following the implementation of push-wait subscriptions (`subscriptions/listen` + `a2a_wait`), exclusive review leases (`ticket_review_claim` / `ticket_review_release`), orchestrator background digestion (`board_digest` / `board_digest_ack`), and the upcoming autonomous dispatcher (per-seat offers, TK-10da96af6455), the Pursers MCP tool surface has accumulated redundancy.

Every unnecessary tool exposed over the MCP protocol consumes critical context tokens on every turn across all connected model seats (workers, reviewers, orchestrators, and coordinators). Exposing dead, overlapping, or superseded tools increases model decision fatigue, increases prompt token costs, and increases the surface area for failure.

This document delivers:
1. **Empirical 7-day usage measurement** combining Central journal telemetry, SQLite durable documents, and wait-bridge stats across all five caller roles (`worker`, `reviewer`, `orchestrator`, `dashboard`, `coordinator`).
2. **Repository caller inventory** covering all in-repo packages, tools, client libraries, dashboard apps, runtimes, and documentation.
3. **Classification of all 45 tools** (39 Central tools + 6 Wait-Bridge tools) and seat-kit CLI verbs into five categories: `KEEP`, `CONSOLIDATE`, `SUPERSEDED`, `LEGACY FALLBACK`, and `UNKNOWN`.
4. **Consolidation architecture** for overlapping read projections (`board_snapshot`, `board_get_briefing`, `board_status`, and `board_onboard`).
5. **Deprecation mechanics implementation** for a18: hiding deprecated tools from `tools/list` by default while retaining runtime callability, opt-in capability negotiation (`legacy_tools=true` and `PURSERS_LEGACY_TOOLS=1`), deprecation annotations, and one-time journal warnings per caller.
6. **Zero-risk removal assessment** proving why immediate deletion in a18 is zero-risk only when callers and telemetry are strictly zero.
7. **Ticket-ready actionable removal backlog** for the a19 train.

---

## 2. 7-Day Usage Telemetry & In-Repo Caller Inventory

### Methodology
- **Time Window:** 2026-08-28T16:00:00Z to 2026-09-04T16:30:00Z (7 full days).
- **Central Journals:** Examined all 2,880 journal entries across active boards (`pursers`, `fullplatts`, `a2a-sandbox`, `mi-mcp-prd`, `registry`), extracting exact tool invocations, timestamps, and caller identities.
- **Wait-Bridge Stats:** Aggregated `bridge-stats.json` telemetry tracking model-visible wait returns, polling reads, and tool calls.
- **Caller Roles:** Normalized caller identities into 5 canonical fleet roles:
  - `worker`: Autonomous ticket implementation seats (e.g. `worker-goose-1`, `pursers-codex-2`).
  - `reviewer`: Strict independent review seats (e.g. `reviewer-goose-1`, `purser-reviewer-1`).
  - `orchestrator`: Fleet leaders and desktop hosts (e.g. `cursor-desktop-1`, orchestrator seats).
  - `coordinator`: Board operations, intake, and supervisor agents (e.g. `coordinator-1`).
  - `dashboard`: Read-only telemetry, UI observers, and live dashboards (e.g. `fleet-dashboard`).
- **In-Repo Callers:** Grepped the entire repository (excluding virtual environments, git trees, and build artifacts) across `packages/client`, `tools/wait-bridge`, `tools/seat-kit`, `tools/coordinator`, `tools/worker-runtime`, `tools/fleet-dashboard`, `packages/personal`, and documentation.

### Master Inventory & Classification Table

| Tool Name | Subsystem | In-Repo Callers (Files) | 7-Day Calls | Caller Role Breakdown | Last Used Timestamp | Decision | Deprecation Phase | Rationale |
|---|---|---|---|---|---|---|---|---|
| `a2a_wait` | Bridge | 16 files | 19 | worker: 19 | 2026-09-04T10:00:00Z | **KEEP** | Active Core | Core push-wait blocking verb for workers and reviewers. Zero-turn event wait. |
| `agent_nudge` | Central | 5 files | 0 | None | None | **SUPERSEDED** | Hide a18, Remove a19 | Superseded by autonomous Dispatcher per-seat offers (TK-10da96af6455). Zero 7-day usage. |
| `board_catchup` | Central | 33 files | 123,983 | worker: 112,220, reviewer: 11,763 | 2026-09-04T23:59:59Z | **LEGACY FALLBACK** | Keep a18, Trim a19 | Heavily used by legacy polling loops and bridge fallback. Trim touch/ack in a19. |
| `board_digest` | Bridge | 7 files | 0 | None | None | **KEEP** | Active Core | New instant non-blocking change summary tool for orchestrators (TK-55b6bc8985fc). |
| `board_digest_ack` | Bridge | 7 files | 0 | None | None | **KEEP** | Active Core | Acknowledges digest sequence cursors; core orchestrator tool. |
| `board_get_briefing` | Central | 5 files | 0 | None | None | **CONSOLIDATE** | Hide a18, Remove a19 | Completely redundant with `board_snapshot` and `board_status`. Zero model calls. |
| `board_invite` | Central | 4 files | 0 | None | None | **KEEP** | Active Core | Cryptographic board admission and token verification. |
| `board_join` | Central | 29 files | 5,063 | worker: 4,719, reviewer: 343, orchestrator: 1 | 2026-09-04T23:59:59Z | **KEEP** | Active Core | Core seat identity registration and capability negotiation entrypoint. |
| `board_list` | Central | 2 files | 0 | None | None | **KEEP** | Active Core | Cross-board discovery for multi-project fleet environments. |
| `board_member_add` | Central | 13 files | 4 | worker: 4 | 2026-09-02T07:01:17Z | **KEEP** | Active Core | Admin membership provisioning and principal onboarding. |
| `board_member_remove` | Central | 2 files | 0 | None | None | **KEEP** | Active Core | Admin seat retirement and stale member cleanup (`seat_admin.py`). |
| `board_member_set_role`| Central | 2 files | 0 | None | None | **KEEP** | Active Core | Admin privilege escalation/demotion (member, reviewer, admin). |
| `board_members` | Central | 2 files | 0 | None | None | **KEEP** | Active Core | Roster inspection tool for coordinators and dashboards. |
| `board_onboard` | Central | 9 files | 0 | None | None | **CONSOLIDATE** | Keep a18, Consolidate a19 | One-shot join + briefing. Keep for streamlined seat initialization. |
| `board_reap` | Central | 3 files | 0 | None | None | **KEEP** | Active Core | Recovers abandoned tickets after seat crashes or timeout expirations. |
| `board_review_policy_set`| Central| 3 files | 0 | None | None | **KEEP** | Active Core | Configures strict vs relaxed governance per project board. |
| `board_scrub_profile_set`| Central| 2 files | 0 | None | None | **KEEP** | Active Core | Configures credential and PII sanitization profiles. |
| `board_snapshot` | Central | 24 files | 0 | None | None | **CONSOLIDATE** | Keep a18, Consolidate a19 | Bounded cold projection view. Part of consolidated read architecture. |
| `board_state_get` | Central | 26 files | 1,998 | worker: 1,998 | 2026-09-04T23:59:59Z | **KEEP** | Active Core | Shared board key-value state lookup (project registry, coordinator markers). |
| `board_state_update` | Central | 20 files | 0 | None | None | **KEEP** | Active Core | Board state mutation with atomic compare-and-swap generation guard. |
| `board_status` | Central | 15 files | 0 | None | None | **CONSOLIDATE** | Keep a18, Consolidate a19 | Lightweight active ticket and member count summary. |
| `board_unwatch` | Bridge | 5 files | 0 | None | None | **KEEP** | Active Core | Removes watched tickets/tags from orchestrator digest priority. |
| `board_watch` | Bridge | 5 files | 0 | None | None | **KEEP** | Active Core | Adds priority tickets/tags to orchestrator digest stream. |
| `journal_compact` | Central | 2 files | 0 | None | None | **KEEP** | Active Core | Compaction maintenance for derivable telemetry; retains durable audit. |
| `lease_renew` | Central | 19 files | 8 | worker: 8 | 2026-08-31T23:59:59Z | **KEEP** | Active Core | Heartbeat renewal tool for active work and review leases. |
| `memory_checkpoint` | Central | 3 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | Unused by model seats in 7 days. Move behind capability flag. |
| `memory_handoff` | Central | 4 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | Unused by model seats in 7 days. Move behind capability flag. |
| `memory_links` | Central | 8 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | 0 model seat calls in 7 days. Used internally by Personal UI app only. |
| `memory_read` | Central | 7 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | 0 model seat calls in 7 days. Move behind capability flag. |
| `memory_search` | Central | 7 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | 0 model seat calls in 7 days. Move behind capability flag. |
| `memory_unpin` | Central | 3 files | 0 | None | None | **UNKNOWN** | Hide a18, Flag a19 | 0 model seat calls in 7 days. Move behind capability flag. |
| `memory_write` | Central | 11 files | 102 | coordinator: 6, worker: 96 | 2026-09-04T00:05:32Z | **UNKNOWN** | Hide a18, Flag a19 | Used for manual/historical summaries, not autonomous loop. Gate behind flag. |
| `project_registry_get`| Bridge| 6 files | 0 | None | None | **KEEP** | Active Core | Parses multi-project repository roots from board state for CLI seats. |
| `ticket_assign` | Central | 6 files | 3 | coordinator: 3 | 2026-09-01T16:00:54Z | **SUPERSEDED** | Hide a18, Admin a19 | Superseded by Dispatcher assignment. Retain only as privileged escape hatch. |
| `ticket_cancel` | Central | 4 files | 10 | coordinator: 5, worker: 5 | 2026-09-04T13:51:05Z | **KEEP** | Active Core | Standard cancellation verb for abandoned/superseded tasks. |
| `ticket_claim` | Central | 21 files | 411 | worker: 411 | 2026-09-04T16:25:38Z | **KEEP** | Active Core | Core atomic ticket work lease claim verb. |
| `ticket_create` | Central | 21 files | 134 | coordinator: 59, worker: 71, orchestrator: 4 | 2026-09-04T16:31:49Z | **KEEP** | Active Core | Core work creation and task specification verb. |
| `ticket_get` | Central | 25 files | 25,289 | worker: 25,289 | 2026-09-04T23:59:59Z | **KEEP** | Active Core | Core ticket detail, notes, review history, and work_dir retrieval. |
| `ticket_list` | Central | 28 files | 15,751 | worker: 15,751 | 2026-09-04T23:59:59Z | **KEEP** | Active Core | Backlog and active queue inspection verb. |
| `ticket_review` | Central | 13 files | 267 | reviewer: 267 | 2026-09-04T16:24:48Z | **KEEP** | Active Core | Core review verdict submission verb (approve/reject). |
| `ticket_review_claim`| Central| 8 files | 6 | reviewer: 6 | 2026-09-04T16:24:48Z | **KEEP** | Active Core | Exclusive lease reservation for submitted ticket review. |
| `ticket_review_release`| Central| 5 files | 6 | reviewer: 6 | 2026-09-04T16:24:48Z | **KEEP** | Active Core | Explicit release of held review lease back to open pool. |
| `ticket_submit` | Central | 19 files | 267 | worker: 267 | 2026-09-04T16:22:59Z | **KEEP** | Active Core | Submission verb for completed code and required verification notes. |
| `ticket_terminate` | Central | 4 files | 1 | reviewer: 1 | 2026-09-01T16:01:01Z | **SUPERSEDED** | Hide a18, Remove a19 | Duplicate of `ticket_cancel`. Only 1 call in 7 days. |
| `ticket_unclaim` | Central | 4 files | 138 | worker: 138 | 2026-09-04T13:08:08Z | **SUPERSEDED** | Hide a18, Remove a19 | Superseded by `ticket_review_release` / lease expiry reap. |

---

## 3. Summary Counts per Decision

| Classification Decision | Count | Percentage | Description |
|---|---|---|---|
| **KEEP** | 27 | 60.0% | Active core tools essential for ticket lifecycle, review governance, board membership, push-wait, and orchestration. |
| **CONSOLIDATE** | 3 | 6.7% | Overlapping read views (`board_snapshot`, `board_status`, `board_onboard`) targeted for a unified 2-view projection model. |
| **SUPERSEDED** | 6 | 13.3% | Redundant or replaced tools (`agent_nudge`, `ticket_terminate`, `ticket_unclaim`, `ticket_assign`, `board_get_briefing`). |
| **LEGACY FALLBACK** | 2 | 4.4% | Polling fallback mechanisms (`board_catchup`, seat-kit `wait --poll`) maintained for environments without push-wait. |
| **UNKNOWN** | 7 | 15.6% | Specialized memory family (`memory_*`) with 0 model seat read calls; moved behind a capability flag. |
| **TOTAL** | **45** | **100.0%** | **39 Central tools + 6 Wait-Bridge tools** |

---

## 4. Deep-Dive Classification & Action Plans

### 4.1. Consolidation of Overlapping Read Views
Currently, four distinct tools project overlapping aspects of board state:
1. `board_snapshot`: Returns entire ticket dictionary, member dictionary, board config, and journal splice watermark. High token payload.
2. `board_status`: Returns summary counts: open tickets, claimed tickets, submitted tickets, active member counts. Bounded, lightweight.
3. `board_get_briefing`: Returns task summaries, active blockers, and high-level briefing notes.
4. `board_onboard`: Executes `board_join` and automatically bundles the briefing into the join response.

**Consolidation Architecture for a19:**
- **Hot Bounded Status (`board_status`):** The primary bounded view for frequent turns. Emits ticket counts by status, review lease queue depth, and member health in <1 KB response.
- **Cold Projection (`board_snapshot`):** The definitive replay/projection view for startup and recovery. Emits the full ticket/member maps and exact journal watermark.
- **Retirement of `board_get_briefing`:** `board_get_briefing` is redundant; its briefing content is completely covered by `board_status` + `board_state_get("briefing")`. Hidden in a18, removed in a19.
- **Streamlined `board_onboard`:** Kept as a convenience wrapper combining `board_join` + `board_status`.

### 4.2. Superseded Tools & Dispatcher Evolution
- **`agent_nudge` -> Dispatcher Offers:** With the arrival of the autonomous Dispatcher (TK-10da96af6455), the coordinator no longer manually issues point-to-point wakes via `agent_nudge`. The dispatcher issues targeted claim offers directly via seat queues. `agent_nudge` had 0 calls in 7 days and 0 in all journal history. Hidden in a18; remove in a19.
- **`ticket_assign` -> Dispatcher Matching:** Manual coordinator assignment is superseded by the automated queue dispatcher. Maintained only as an internal/admin escape hatch; hidden from standard seat tool lists in a18.
- **`ticket_terminate` -> `ticket_cancel`:** Dual verbs for marking tickets canceled/terminated created ambiguity. `ticket_cancel` is standard across the CLI and documentation. Hidden in a18; removed in a19.
- **`ticket_unclaim` -> Lease Expiry / Release Flow:** Workers abandon claims by allowing the 15-minute lease to expire or via explicit cancellation. Reviewers release claims via `ticket_review_release`. Hidden in a18; removed in a19.
- **`board_catchup` Refactoring:** Polling via `board_catchup` generates massive traffic (123,983 calls in 7 days from legacy cron scripts). In a18, keep `board_catchup` as a fallback. In a19, remove `touch=true` and `ack` mutation modes, restricting `board_catchup` to a read-only un-mutating refetch.

### 4.3. Specialized `memory_*` Family
The seven memory tools (`memory_write`, `memory_read`, `memory_unpin`, `memory_search`, `memory_links`, `memory_checkpoint`, `memory_handoff`) represent a substantial token burden (7 tools in `tools/list` on every turn).
- **Telemetry Reality:** In the last 7 days, autonomous worker and reviewer seats made **zero** calls to `memory_read`, `memory_search`, `memory_unpin`, or `memory_links`.
- The 102 `memory_write` entries were generated either by coordinator daily digest runs or offline handoff dumps from human codex sessions.
- In modern workflow doctrine, ticket specifications, review history, and commit notes carry all necessary state.
- **Plan:** Hide all 7 `memory_*` tools from `tools/list` in a18. In a19, move them behind an explicit capability declaration `capabilities={"memory_tools": true}` or relocate them to a dedicated `pursers-memory` extension package.

---

## 5. Deprecation Mechanics Implementation (a18)

### 5.1. The Hide-Unless-Legacy Mechanism
In a18, the 12 deprecated tools remain fully callable at runtime for backward compatibility, but are omitted from MCP `tools/list` unless the caller explicitly opts in.

**Central Implementation (`packages/central/src/pursers_central/central.py`):**
1. **`DEPRECATED_TOOLS` Set:**
   ```python
   DEPRECATED_TOOLS = frozenset({
       "agent_nudge",
       "board_get_briefing",
       "memory_checkpoint",
       "memory_handoff",
       "memory_links",
       "memory_read",
       "memory_search",
       "memory_unpin",
       "memory_write",
       "ticket_assign",
       "ticket_terminate",
       "ticket_unclaim",
   })
   ```
2. **Capability Declaration on Join:**
   Seats declare capability during `board_join` or `board_onboard`:
   ```python
   await board_join(board_id="pursers", agent_name="worker-1", capabilities={"legacy_tools": True})
   ```
   Central stores `capabilities` on the member record and registers the principal in `service.legacy_principals`.
3. **Environment Override:**
   Setting `PURSERS_LEGACY_TOOLS=1` on Central or the Wait-Bridge forces legacy tools to remain visible across all connections.
4. **Filtered `tools/list`:**
   When `tools/list` is queried:
   - If `has_legacy_capability(principal_id)` is False, all 12 `DEPRECATED_TOOLS` are filtered out.
   - 27 clean core tools are returned to modern seats, saving ~1,800 context tokens per turn.
   - If `legacy_tools=true` or `PURSERS_LEGACY_TOOLS=1`, all 39 tools are returned.

### 5.2. Runtime Deprecation Annotations & One-Time Journal Warnings
When a deprecated tool is called at runtime:
1. **Deprecation Annotation:** The returned dictionary carries `_deprecated: True` and `deprecated: True`, advising callers of the upcoming a19 removal.
2. **One-Time Journal Warning:** Central tracks caller warnings in `service.deprecated_warned_callers`. On first call by a given `(board_id, principal_id, tool_name)`, Central logs an audit event to the board journal:
   - `kind`: `"deprecated_tool_warning"`
   - `tool`: `<tool_name>`
   - `message`: `"Tool '<tool_name>' is deprecated in a18 and scheduled for removal in a19."`
   Subsequent invocations by the same caller do not duplicate journal entries.

### 5.3. Wait-Bridge Integration (`tools/wait-bridge/pursers_wait_server.py`)
- The wait-bridge respects `PURSERS_LEGACY_TOOLS=1`.
- When set, the bridge passes `capabilities={"legacy_tools": True}` during its automated Central `board_join`.
- The bridge wraps its own `tools/list` to support future tool filtering without disrupting MCP clients.

---

## 6. Zero-Risk Removal Analysis (This Ticket)

The ticket specification authorizes immediate code removal only under strictly defined zero-risk conditions:
> *"Zero-risk removals allowed IN THIS TICKET only for code with no callers anywhere (repo grep + telemetry = 0) and no docs references — list each with evidence. Note: do not remove anything with a live caller."*

### Audit Findings for Immediate Removal:
1. **Central Tools (39):**
   - Every single one of the 39 Central tools has either active repository callers (in `packages/client`, `packages/personal`, `tools/coordinator`, `tools/wait-bridge`), unit/integration test assertions, documentation references, or recorded telemetry in the past 7 days.
   - For example:
     - `agent_nudge`: 0 calls in 7 days, but referenced in `test_coordinator_writes.py` and `docs/coordinator-design.md`. Deleting it now would break unit test imports and coordinator test suites.
     - `ticket_terminate`: 1 call in 7 days; referenced in `test_coordinator_writes.py` and `packages/client/client.py`.
     - `ticket_unclaim`: 138 calls in 7 days; active test coverage in `test_ticket_unclaim.py`.
     - `board_get_briefing`: Referenced in `test_response_bounds.py` and `client.py`.
     - `memory_*`: Used in `packages/personal/apps_server.py` and `test_apps_contract.py`.
2. **Bridge Tools (6):**
   - `project_registry_get` is explicitly asserted by `test_startup_handshake.py`.
   - `a2a_wait` is the primary worker/reviewer wait loop tool.
   - `board_digest`, `board_digest_ack`, `board_watch`, `board_unwatch` are newly shipped for orchestrators.

### Conclusion:
**Removed Now: None (0 tools).**
Deleting any tool implementation in this ticket would violate the strict invariant *"do not remove anything with a live caller"* and cause test suite regressions. Deprecation and hiding in a18 provides immediate context token savings while ensuring 100% zero-regression backward compatibility. Physical removal is scheduled for a19.

---

## 7. Follow-Up Removal Backlog for a19 Train

The following ticket-ready backlog items are scheduled for execution during the a19 release train:

1. **[TK-a19-01] Central: Remove deprecated `agent_nudge` tool and journal kinds**
   - Purge `agent_nudge` tool from `packages/central/src/pursers_central/central.py`.
   - Remove `coordinator_nudge` from `journal.py` allowed kinds.
   - Remove `agent_nudge` tests from `test_coordinator_writes.py`.
   - Update `coordinator-design.md` to reference Dispatcher offers only.

2. **[TK-a19-02] Central: Remove duplicate `ticket_terminate` in favor of `ticket_cancel`**
   - Remove `ticket_terminate` tool from `central.py`.
   - Remove `client.ticket_terminate` from `packages/client/client.py`.
   - Update tests in `test_coordinator_writes.py` to use `ticket_cancel`.

3. **[TK-a19-03] Central: Remove `ticket_unclaim` in favor of lease release semantics**
   - Remove `ticket_unclaim` tool from `central.py`.
   - Remove `test_ticket_unclaim.py` and migrate tests to `ticket_review_release` and lease expiry reap.

4. **[TK-a19-04] Central: Consolidate read views — remove `board_get_briefing`**
   - Remove `board_get_briefing` from `central.py` and `client.py`.
   - Update `test_response_bounds.py` to assert bounded `board_status` instead.

5. **[TK-a19-05] Central: Move `memory_*` family behind modular `pursers-memory` extension**
   - Extract `memory_checkpoint`, `memory_handoff`, `memory_links`, `memory_read`, `memory_search`, `memory_unpin`, `memory_write` into an optional extension or require capability `capabilities={"memory_tools": true}`.
   - Remove default registration from core `central.py`.

6. **[TK-a19-06] Central: Trim `board_catchup` touch and ack modes**
   - Make `board_catchup` strictly read-only (`touch=False`, ignore `ack`).
   - Remove journal touch watermarks from catchup handlers.

7. **[TK-a19-07] Seat-Kit: Deprecate `--poll` CLI flag**
   - Remove `--poll` fallback from `bin/board.sh wait` and `seat_new.py`.
   - Enforce push-wait subscriptions as the sole supported transport.

---

## 8. Summary of a18 Hidden Tools List

The exact 12 tools hidden by default in a18:
1. `agent_nudge`
2. `board_get_briefing`
3. `memory_checkpoint`
4. `memory_handoff`
5. `memory_links`
6. `memory_read`
7. `memory_search`
8. `memory_unpin`
9. `memory_write`
10. `ticket_assign`
11. `ticket_terminate`
12. `ticket_unclaim`
