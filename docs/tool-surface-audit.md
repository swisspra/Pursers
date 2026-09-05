# Tool Surface Audit for Pursers a18 Release Train

**Date:** 2026-09-04<br>
**Author:** AAIF / Pursers Fleet Engineering (TK-2ffa16368cbb)<br>
**Status:** a21 removal implemented by TK-7d99c07860bd<br>
**Target:** a18 audit retained; a21 removes three retired tools and keeps one admin escape hatch

---

## 1. Executive Summary & Purpose

Following the implementation of push-wait subscriptions (`subscriptions/listen` + `a2a_wait`), exclusive review leases (`ticket_review_claim` / `ticket_review_release`), orchestrator background digestion (`board_digest` / `board_digest_ack`), and the upcoming autonomous dispatcher (per-seat offers, TK-10da96af6455), the Pursers MCP tool surface has accumulated redundancy.

Every unnecessary tool exposed over the MCP protocol consumes critical context tokens on every turn across all connected model seats (workers, reviewers, orchestrators, and coordinators). Exposing dead, overlapping, or superseded tools increases model decision fatigue, increases prompt token costs, and increases the surface area for failure.

This document delivers:
1. **Empirical 7-day usage measurement** combining Central journal telemetry, SQLite durable documents, and wait-bridge stats across all five caller roles (`worker`, `reviewer`, `orchestrator`, `dashboard`, `coordinator`).
2. **Repository caller inventory** covering all in-repo packages, tools, client libraries, dashboard apps, runtimes, and documentation.
3. **Classification of all 45 tools** (39 Central tools + 6 Wait-Bridge tools) and seat-kit CLI verbs into five categories: `KEEP`, `CONSOLIDATE`, `SUPERSEDED`, `LEGACY FALLBACK`, and `UNKNOWN`.
4. **Consolidation architecture** for bounded read projections (`board_snapshot`, `board_status`, and `board_onboard`).
5. **Deprecation mechanics implementation** for the retained `ticket_assign` admin escape hatch: hide it from `tools/list` by default while retaining opt-in capability negotiation (`capabilities={"legacy_tools": true}` and `PURSERS_LEGACY_TOOLS=1`), standard `ToolAnnotations` deprecation hints with `_meta`, and one-time sequenced journal warnings per caller with durable deduplication across restarts.
6. **Zero-risk removal assessment** proving why immediate deletion in a18 is zero-risk only when callers and telemetry are strictly zero.
7. **Updated actionable backlog** after the a21 removal.

---

## 2. 7-Day Usage Telemetry & In-Repo Caller Inventory

### Methodology & Source Boundaries
- **Time Window:** 2026-08-28T16:00:00Z to 2026-09-04T16:30:00Z (7 full days).
- **Central Durable Database:** Central storage at `.private-arm/central-data/board.sqlite3` (`documents` table):
  - `journals/%` rows: All 2,880 historical journal entries across active boards (`pursers`, `fullplatts`, `a2a-sandbox`, `mi-mcp-prd`, `registry`), extracting exact tool invocations, ISO-8601 UTC timestamps (`occurred_at`), and actor identities.
  - `boards/%` records: Roster of 159 joined member records mapping `agent_id` to human/agent names and permissions.
- **Wait-Bridge Stats:** Telemetry from `pursers-wait-bridge/bridge-stats.json` and `tools/wait-bridge/bridge-stats.json`:
  - `days` block: Daily aggregate call counters per seat and tool (granularity: calendar date).
  - `model_wait` block: Hourly bucket counters for `a2a_wait` outcomes (granularity: hourly bucket).
- **Caller Roles:** Normalized caller identities into 5 canonical fleet roles:
  - `worker`: Autonomous ticket implementation seats (e.g. `worker-goose-1`, `pursers-codex-2`).
  - `reviewer`: Strict independent review seats (e.g. `reviewer-goose-1`, `purser-reviewer-1`).
  - `orchestrator`: Fleet leaders and desktop hosts (e.g. `cursor-desktop-1`, orchestrator seats).
  - `coordinator`: Board operations, intake, and supervisor agents (e.g. `coordinator-1`).
  - `dashboard`: Read-only telemetry, UI observers, and live dashboards (e.g. `fleet-dashboard`).
- **In-Repo Callers:** Scanned the entire repository (excluding `.git`, `.venv`, `.venv2`, `build`, `dist`, `.pytest_cache`) across `packages/client`, `tools/wait-bridge`, `tools/seat-kit`, `tools/coordinator`, `tools/worker-runtime`, `tools/fleet-dashboard`, `packages/personal`, and documentation.

### Master Inventory & Classification Table

| Tool Name | Subsystem | In-Repo Callers (Files) | 7-Day Calls | Caller Role Breakdown | Last Used (Granularity / Source) | Decision | Deprecation Phase | Rationale |
|---|---|---|---|---|---|---|---|---|
| `a2a_wait` | Bridge | 16 files | 19 | worker: 19 | 2026-09-04T10:00:00Z (bridge hourly bucket) | **KEEP** | Active Core | Core push-wait blocking verb for workers and reviewers. Zero-turn event wait. |
| `agent_nudge` | Central | 5 files | 0 | None | Coordinator compatibility caller | **REMOVED** | Removed by TK-7d99c07860bd | Autonomous Dispatcher per-seat offers are the sole targeted wake path. |
| `board_catchup` | Central | 33 files | 123,983 | worker: 112,220, reviewer: 11,763 | 2026-09-04 (daily aggregate) | **LEGACY FALLBACK** | Keep a18, Trim a19 | Heavily used by legacy polling loops and bridge fallback. Trim touch/ack in a19. |
| `board_digest` | Bridge | 7 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | New instant non-blocking change summary tool for orchestrators (TK-55b6bc8985fc). |
| `board_digest_ack` | Bridge | 7 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Acknowledges digest sequence cursors; core orchestrator tool. |
| `board_get_briefing` | Central | Personal app, client, tests | 0 model calls | Personal dashboard read path | None (0 model calls recorded in 7d window) | **REMOVED** | Removed by TK-7d99c07860bd | Replaced by bounded `board_snapshot` + `board_status`. |
| `board_invite` | Central | 4 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Cryptographic board admission and token verification. |
| `board_join` | Central | 29 files | 5,063 | worker: 4,719, reviewer: 343, orchestrator: 1 | 2026-09-04 (daily aggregate) | **KEEP** | Active Core | Core seat identity registration and capability negotiation entrypoint. |
| `board_list` | Central | 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Cross-board discovery for multi-project fleet environments. |
| `board_member_add` | Central | 13 files | 4 | worker: 4 | 2026-09-02T07:01:17Z (journal event) | **KEEP** | Active Core | Admin membership provisioning and principal onboarding. |
| `board_member_remove` | Central | 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Admin seat retirement and stale member cleanup (`seat_admin.py`). |
| `board_member_set_role`| Central | 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Admin privilege escalation/demotion (member, reviewer, admin). |
| `board_members` | Central | 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Roster inspection tool for coordinators and dashboards. |
| `board_onboard` | Central | 9 files | 0 | None | None (0 calls recorded in 7d window) | **CONSOLIDATE** | Keep a18, Consolidate a19 | One-shot join + briefing. Keep for streamlined seat initialization. |
| `board_reap` | Central | 3 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Recovers abandoned tickets after seat crashes or timeout expirations. |
| `board_review_policy_set`| Central| 3 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Configures strict vs relaxed governance per project board. |
| `board_scrub_profile_set`| Central| 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Configures credential and PII sanitization profiles. |
| `board_snapshot` | Central | 24 files | 0 | None | None (0 calls recorded in 7d window) | **CONSOLIDATE** | Keep a18, Consolidate a19 | Bounded cold projection view. Part of consolidated read architecture. |
| `board_state_get` | Central | 26 files | 1,998 | worker: 1,998 | 2026-09-04 (daily aggregate) | **KEEP** | Active Core | Shared board key-value state lookup (project registry, coordinator markers). |
| `board_state_update` | Central | 20 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Board state mutation with atomic compare-and-swap generation guard. |
| `board_status` | Central | 15 files | 0 | None | None (0 calls recorded in 7d window) | **CONSOLIDATE** | Keep a18, Consolidate a19 | Lightweight active ticket and member count summary. |
| `board_unwatch` | Bridge | 5 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Removes watched tickets/tags from orchestrator digest priority. |
| `board_watch` | Bridge | 5 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Adds priority tickets/tags to orchestrator digest stream. |
| `journal_compact` | Central | 2 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Compaction maintenance for derivable telemetry; retains durable audit. |
| `lease_renew` | Central | 19 files | 8 | worker: 8 | 2026-08-31 (daily aggregate) | **KEEP** | Active Core | Heartbeat renewal tool for active work and review leases. |
| `memory_checkpoint` | Central | 3 files | 0 | None | Personal app caller inventory | **KEEP** | Active Personal | Personal exposes the checkpoint workflow to model-visible app tools. Model-seat telemetry did not measure app calls. |
| `memory_handoff` | Central | 4 files | 0 | None | Personal app caller inventory | **KEEP** | Active Personal | Personal exposes the handoff workflow to model-visible app tools. Model-seat telemetry did not measure app calls. |
| `memory_links` | Central | 8 files | 0 | None | Personal dashboard live caller | **KEEP** | Active Personal | Powers the Personal UI link projection and is also exposed as an app tool. |
| `memory_read` | Central | 7 files | 0 | None | Personal app caller inventory | **KEEP** | Active Personal | Personal exposes bounded memory reading to model-visible app tools. |
| `memory_search` | Central | 7 files | 0 | None | Personal app caller inventory | **KEEP** | Active Personal | Personal exposes bounded memory search to model-visible app tools. |
| `memory_unpin` | Central | 3 files | 0 | None | Personal app caller inventory | **KEEP** | Active Personal | Personal exposes memory unpinning to model-visible app tools. |
| `memory_write` | Central | 11 files | 102 | coordinator: 6, worker: 96 | 2026-09-04T00:05:32Z (journal event) | **KEEP** | Active Core | 102 calls in 7-day window. Retained as visible core tool per measured traffic. |
| `project_registry_get`| Bridge| 6 files | 0 | None | None (0 calls recorded in 7d window) | **KEEP** | Active Core | Parses multi-project repository roots from board state for CLI seats. |
| `ticket_assign` | Central | 6 files | 3 | coordinator: 3 | 2026-09-01T16:00:54Z (journal event) | **SUPERSEDED** | Hide a18, TK-7d99c07860bd | Superseded by Dispatcher assignment, but retained as the coordinator's privileged compatibility escape hatch. |
| `ticket_cancel` | Central | 4 files | 10 | coordinator: 5, worker: 5 | 2026-09-04T13:51:05Z (journal event) | **KEEP** | Active Core | Standard cancellation verb for abandoned/superseded tasks. |
| `ticket_claim` | Central | 21 files | 411 | worker: 411 | 2026-09-04T16:25:38Z (journal event) | **KEEP** | Active Core | Core atomic ticket work lease claim verb. |
| `ticket_create` | Central | 21 files | 134 | coordinator: 59, worker: 71, orchestrator: 4 | 2026-09-04T16:31:49Z (journal event) | **KEEP** | Active Core | Core work creation and task specification verb. |
| `ticket_get` | Central | 25 files | 25,289 | worker: 25,289 | 2026-09-04 (daily aggregate) | **KEEP** | Active Core | Core ticket detail, notes, review history, and work_dir retrieval. |
| `ticket_list` | Central | 28 files | 15,751 | worker: 15,751 | 2026-09-04 (daily aggregate) | **KEEP** | Active Core | Backlog and active queue inspection verb. |
| `ticket_review` | Central | 13 files | 267 | reviewer: 267 | 2026-09-04T16:24:48Z (journal event) | **KEEP** | Active Core | Core review verdict submission verb (approve/reject). |
| `ticket_review_claim`| Central| 8 files | 6 | reviewer: 6 | 2026-09-04T16:24:48Z (journal event) | **KEEP** | Active Core | Exclusive lease reservation for submitted ticket review. |
| `ticket_review_release`| Central| 5 files | 6 | reviewer: 6 | 2026-09-04T16:24:48Z (journal event) | **KEEP** | Active Core | Explicit release of held review lease back to open pool. |
| `ticket_submit` | Central | 19 files | 267 | worker: 267 | 2026-09-04T16:22:59Z (journal event) | **KEEP** | Active Core | Submission verb for completed code and required verification notes. |
| `ticket_terminate` | Central | 4 files | 1 | reviewer: 1 | 2026-09-01T16:01:01Z (journal event) | **REMOVED** | Removed by TK-7d99c07860bd | Duplicate of `ticket_cancel`; callers use the standard cancellation verb. |
| `ticket_unclaim` | Central | 4 files | 138 | worker: 138 | 2026-09-04T13:08:08Z (journal event) | **KEEP** | Active Core | 138 live worker calls in 7-day window. Retained as visible core tool. |

---

## 3. Summary Counts per Decision

*Note: The original 45-tool inventory now contains 42 active/deprecated tools and 3 removed rows.*

| Classification Decision | Count | Percentage | Description |
|---|---|---|---|
| **KEEP** | 37 | 82.2% | Active core and Personal app tools essential for ticket lifecycle, memory UI, review governance, board membership, push-wait, and orchestration. |
| **CONSOLIDATE** | 3 | 6.7% | Bounded views (`board_snapshot`, `board_status`, `board_onboard`) retain distinct hot, cold, and onboarding roles. |
| **SUPERSEDED** | 1 | 2.2% | `ticket_assign` remains only as a privileged compatibility escape hatch. |
| **LEGACY FALLBACK** | 1 | 2.2% | Polling fallback mechanism (`board_catchup`) is maintained for environments without push-wait. |
| **REMOVED** | 3 | 6.7% | `agent_nudge`, `board_get_briefing`, and `ticket_terminate` were physically removed. |
| **UNKNOWN** | 0 | 0.0% | No tools remain unclassified after accounting for Personal app callers. |
| **TOTAL** | **45** | **100.0%** | **39 Central tools + 6 Wait-Bridge tools** |

---

## 4. Deep-Dive Classification & Action Plans

### 4.1. Consolidation of Overlapping Read Views
Three tools project complementary bounded aspects of board state:
1. `board_snapshot`: Returns entire ticket dictionary, member dictionary, board config, and journal splice watermark. High token payload.
2. `board_status`: Returns summary counts: open tickets, claimed tickets, submitted tickets, active member counts. Bounded, lightweight.
3. `board_onboard`: Executes `board_join` and bundles bounded startup context.

**Consolidation Architecture for a19:**
- **Hot Bounded Status (`board_status`):** The primary bounded view for frequent turns. Emits ticket counts by status, review lease queue depth, and member health in <1 KB response.
- **Cold Projection (`board_snapshot`):** The definitive replay/projection view for startup and recovery. Emits the full ticket/member maps and exact journal watermark.
- The redundant standalone briefing view was removed; `board_status`, `board_snapshot`, and `board_state_get("briefing")` cover its read paths.
- **Streamlined `board_onboard`:** Kept as a convenience wrapper combining `board_join` + `board_status`.

### 4.2. Superseded Tools & Dispatcher Evolution
- **Dispatcher Offers:** Targeted wake-up is owned exclusively by the autonomous Dispatcher (TK-10da96af6455); the duplicate Central nudge tool and coordinator stage-one mutation were removed.
- **`ticket_assign` escape hatch:** Automated matching is primary, while manual assignment remains hidden and restricted to admin membership plus `board:coordinate`.
- **Cancellation:** `ticket_cancel` is the single role-authorized cancellation verb; the duplicate termination tool and client wrapper were removed.
- **`ticket_unclaim` Preservation:** Unlike reviewer leases which possess `ticket_review_release`, workers currently rely on `ticket_unclaim` for voluntary claim releases before TTL expiry (138 live calls in 7 days). Retained visible in a18 until an explicit worker lease release replacement is deployed.
- **`board_catchup` Refactoring:** Polling via `board_catchup` generates massive traffic (123,983 calls in 7 days from legacy cron scripts). In a18, keep `board_catchup` as a fallback. In a19, remove `touch=true` and `ack` mutation modes, restricting `board_catchup` to a read-only un-mutating refetch.

### 4.3. Specialized `memory_*` Family
The memory family consists of seven tools and is classified **KEEP**.
- **Telemetry correction:** The original measurement covered model seats, not MCP Apps. `memory_write` recorded **102 live model-seat calls**, while the other six tools showed zero model-seat calls.
- **Personal caller inventory:** `packages/personal/src/pursers_personal/apps_server.py` allow-lists and exposes all six tools. `memory_links` additionally powers the dashboard's bounded link projection. These are shipped callers, so the zero model-seat count cannot support deprecation or removal.
- **Decision:** Keep all seven memory tools visible by default. Remove the six read/workflow tools from `DEPRECATED_TOOLS`; do not emit deprecation warnings for legitimate Personal traffic.

---

## 5. Deprecation Mechanics Implementation (a18)

### 5.1. The Hide-Unless-Legacy Mechanism
Exactly one deprecated tool remains callable at runtime for backward compatibility, but is omitted from MCP `tools/list` unless the calling seat explicitly opts in.

**Central Implementation (`packages/central/src/pursers_central/central.py`):**
1. **`DEPRECATED_TOOLS` Set:**
   ```python
   DEPRECATED_TOOLS = frozenset({"ticket_assign"})
   ```
2. **Seat-Scoped Capability Declaration:**
   Capabilities are registered per seat, not cached globally across the principal:
   ```python
   await board_join(board_id="pursers", agent_name="worker-1", capabilities={"legacy_tools": True})
   ```
   Central stores `capabilities` directly in `document["members"][agent_id]["capabilities"]` in SQLite.
   When `tools/list` runs, Central inspects `client_info.name` (the seat name) and checks `has_seat_legacy_capability(principal_id, agent_name)`.
   If a reviewer and a worker share the same bearer token / principal, the worker opting in does NOT pollute the reviewer's clean tool surface.
3. **Environment Override:**
   Setting `PURSERS_LEGACY_TOOLS=1` forces legacy tools to remain visible across all connections.
4. **ToolAnnotations Hints:**
   When legacy tools are listed, each deprecated tool carries standard MCP annotations:
   `Tool.annotations = ToolAnnotations(title="[DEPRECATED] <name> is an admin coordination escape hatch")`
   along with `Tool.meta = {"deprecated": True}`.

### 5.2. Post-Authorization Deprecation Warnings & Durable Dedupe
When a deprecated tool is called at runtime:
1. **Post-Authorization Execution:** The tool function executes first. If the caller lacks authorization (e.g. outsider on unjoined board), `PermissionError` is raised before any warning logic is reached. Denied calls produce **zero** mutations and **zero** journal events.
2. **Durable deduplication:** Central atomically keys the warning by `tool + caller_principal_id + caller_agent_name` in the journal transaction. The journal keeps at most 4,096 identities and evicts the oldest sequence first. Its bounded summary survives restart and compaction.
3. **Result Metadata:** The returned dictionary carries `_deprecated: True` and `deprecated: True` as an additional compatibility signal.

---

## 6. Authorized Removal Result

TK-7d99c07860bd completed the coordinated removal after its dependency settled
the Personal memory family:

1. `agent_nudge` was removed from Central after the coordinator stopped
   producing stage-one nudge actions; Dispatcher offers remain the wake path.
2. `board_get_briefing` was removed from Central and the client after bounded
   status/snapshot consumers and tests no longer called it.
3. `ticket_terminate` was removed from Central and the client; cancellation
   callers use `ticket_cancel`.
4. `ticket_assign` remains intentionally deprecated and hidden, with admin
   membership plus `board:coordinate` enforcement and durable warning coverage.
5. The Personal `memory_*` family remains active and visible.

---

## 7. Follow-Up Removal Backlog

The following ticket-ready backlog items are scheduled for execution during the a19 release train:

1. **[COMPLETED: TK-7d99c07860bd]** Remove `agent_nudge`,
   `board_get_briefing`, and `ticket_terminate` after migrating shipped callers
   and tests.

2. **[WITHDRAWN] Keep the `memory_*` family visible for Personal**
   - Personal is the intended caller for `memory_checkpoint`, `memory_handoff`, `memory_links`, `memory_read`, `memory_search`, and `memory_unpin`.
   - Reconsider modularization only with an app migration plan and replacement projection coverage.

3. **[TK-a19-05] Central: Trim `board_catchup` touch and ack modes**
   - Make `board_catchup` strictly read-only (`touch=False`, ignore `ack`).
   - Remove journal touch watermarks from catchup handlers.

4. **[TK-a19-06] Seat-Kit: Deprecate `--poll` CLI flag**
   - Remove `--poll` fallback from `bin/board.sh wait` and `seat_new.py`.
   - Enforce push-wait subscriptions as the sole supported transport.

---

## 8. Current Hidden Tool List

The sole tool hidden by default is `ticket_assign`. It remains available only
for the documented admin/`board:coordinate` escape hatch when legacy capability
negotiation is enabled.
