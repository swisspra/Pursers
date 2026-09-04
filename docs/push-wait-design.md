# Push-wait host profiles and re-arm design

## สรุปภาษาไทย

เอกสารนี้กำหนดแนวทางเปลี่ยน Pursers จากการ poll เป็น push-wait โดยยังคง `board://<board_id>/journal` เป็นสัญญาณปลุก แต่ถือ notification เป็นเพียง cue เท่านั้น หลังตื่นต้องดึงข้อมูลจริงใหม่ผ่าน read path แบบ pure ที่ไม่แตะ activity, cursor acknowledgement หรือ lease ใด ๆ ส่วน seat ที่ว่างต้องไม่เรียก Central เลย และ seat ที่ถือ ticket เรียกได้เฉพาะ `lease_renew` ตามรอบของ lease

ผล probe บน Codex Desktop build หนึ่งยืนยันว่า `tool_timeout_sec = 620` รองรับ MCP tool call แบบเงียบประมาณ 605 วินาทีได้จริง จึงไม่ถูกตัดตายตัวที่ 600 วินาที อย่างไรก็ตามผลนี้ยืนยันเฉพาะ build และค่าที่ทดสอบ ไม่ได้พิสูจน์ว่าไม่มีเพดาน เอกสารกำหนด profile ต่อ host, สูตรเผื่อเวลาและ re-arm, progress cadence, การนับ model-visible wait return/bytes และลำดับ rollout สำหรับ Central, wait bridge, seat-kit และ worker runtime

## Status vocabulary and scope

- **EXISTS** means behavior present in the baseline at commit `67dc49ac8154ae4e4bd6b9361b36df8a53a67484`, a documented host behavior, or an operator-supplied deployment fact named below.
- **PROPOSED** means a design contract to implement in the four follow-up tickets. It is not a claim about current behavior.
- This note does not use MCP Tasks. The Python MCP SDK 2.1.1 exposes the MCP 2026-07-28 subscription/listen primitives, while its Tasks support is types-only in the deployment baseline.

## Host profile matrix

Host timeouts are deadlines around the MCP tool call. A server progress notification may protect an idle timer when the host explicitly documents that behavior; it never changes the hard wall-clock deadline unless the host explicitly says so.

| Host profile | Hard tool timeout | Configuration / known maximum | Idle or no-progress timeout | Effect of progress | Auto-background and wake behavior | Raw server notification behavior | Recommended Pursers profile |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Codex Desktop | **EXISTS:** default MCP tool timeout is 60s. A local Desktop probe completed a silent 605s call with a configured 620s timeout. | **EXISTS:** `mcp_servers.<id>.tool_timeout_sec` is configurable. No maximum is documented. The probe proves `>600s` for the tested build, not an unlimited value. | **EXISTS:** no separate idle/no-progress timer is documented or observed in the silent probe. | **PROPOSED:** do not depend on progress to extend the deadline. | **EXISTS:** the 605s call remained attached to the same app-server request; it did not auto-background. Completion returned normally. | **EXISTS:** no verified host contract says an arbitrary resource update starts a model turn. **PROPOSED:** keep one `a2a_wait` call active; its return is the wake. | **PROPOSED:** `tool_timeout_sec=620`, `block_s=560`. Keep the existing compatibility profile `230/200` where configuration cannot yet change. |
| Codex CLI | **EXISTS:** default MCP tool timeout is 60s through the same Codex configuration surface. | **EXISTS:** `tool_timeout_sec` is configurable; no maximum is documented. Desktop is directly probed, CLI is source/configuration-equivalent but not separately timed in this ticket. | **EXISTS:** no separate documented idle timer. | **PROPOSED:** do not rely on progress for deadline extension. | **EXISTS:** no documented automatic conversion of a long foreground MCP call into a background task. | **EXISTS:** no verified contract makes a raw resource update a new model turn. **PROPOSED:** `a2a_wait` must return for the model to act. | **PROPOSED:** `620/560`, subject to a runner-specific probe before making it the fleet default. |
| Goose CLI, bundled Developer extension shell | **EXISTS:** the documented operation timeout default is 300s. | **EXISTS:** extension `timeout` is configurable in seconds; no maximum is documented. | **EXISTS:** no separate documented no-progress timeout. | **PROPOSED:** do not rely on progress to extend the operation timeout. | **EXISTS:** no documented auto-background/wake contract for a long Developer extension operation. | **EXISTS:** the bundled shell operation is not an MCP subscription channel, so MCP server notifications are not applicable to it. **PROPOSED:** its tool result is the wake boundary. | **PROPOSED:** default `300/270`; an opt-in long profile may use `3600/3540` after a local host probe. |
| Goose CLI, MCP extension call | **EXISTS:** the documented stdio/HTTP extension timeout default is 300s. | **EXISTS:** per-extension `timeout` is configurable in seconds; no maximum is documented. | **EXISTS:** no separate documented no-progress timeout. | **PROPOSED:** notifications may be transported, but must not be assumed to extend the host deadline. | **EXISTS:** no documented automatic backgrounding contract. | **EXISTS:** the official configuration guide gives no contract that resource updates start a model turn. **PROPOSED:** an active `a2a_wait` converts the cue into a bounded tool return. | **PROPOSED:** default `300/270`; opt-in `3600/3540` after probe. |
| Claude Code interactive | **EXISTS:** `MCP_TOOL_TIMEOUT` defaults to about 100,000s (about 28h) and is a hard wall-clock timeout. | **EXISTS:** configurable in milliseconds; no lower operational ceiling is documented. | **EXISTS:** stdio MCP has a 30m idle timeout by default. | **EXISTS:** progress notifications reset the stdio idle timer, but do not extend `MCP_TOOL_TIMEOUT`. | **EXISTS:** foreground MCP calls exceeding `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` (default 120,000ms) automatically background and later deliver a task notification. | **EXISTS:** the documented model wake is the completed background tool's task notification, not an arbitrary resource update. **PROPOSED:** `a2a_wait` completion is the wake. | **PROPOSED:** use a 6h operational call deadline and `block_s=21,540`; emit progress every 300s. Re-arm immediately after every cue, timeout, or recoverable error. |
| Claude Code headless | **EXISTS:** the same hard and stdio idle timeouts apply. | **EXISTS:** `MCP_TOOL_TIMEOUT` and MCP idle configuration are available. | **EXISTS:** 30m stdio idle by default. | **EXISTS:** progress resets idle, not the hard deadline. | **EXISTS:** noninteractive mode does not auto-background unless `CLAUDE_AUTO_BACKGROUND_TASKS=1`. A headless worker must not assume an interactive task notification. | **EXISTS:** no documented raw-resource-update model wake exists in headless mode. **PROPOSED:** the runner owns the wait/re-arm loop and invokes the model only for actionable work. | **PROPOSED:** explicitly enable the desired background policy or keep the call foreground; use 6h/21,540s with 300s progress. |
| Claude Desktop | **EXISTS:** deployment baseline supplied for this design is approximately 240s; public reports show build/platform variance. | **EXISTS:** the deployment baseline reports that timeout configuration is ignored. Treat the practical maximum as unknown until each build is probed. | **EXISTS:** no separate dependable idle timer is established. | **PROPOSED:** do not rely on progress. | **EXISTS:** no documented auto-background/wake contract comparable to Claude Code. | **EXISTS:** no verified contract says a resource update starts a model turn. **PROPOSED:** keep an active `a2a_wait`; its completion is the wake. | **PROPOSED:** validated 240s-class builds use `block_s=200` with a 40s profile margin. Unknown builds must probe before enabling push-wait. |
| Headless API worker | **EXISTS:** no interactive MCP host timeout is inherent; the worker process and its HTTP/runtime infrastructure define deadlines. | **EXISTS:** runner, proxy, and deployment limits are environment-specific. | **EXISTS:** environment-specific connection and process idleness only. | **PROPOSED:** application keepalive may protect transports, but must not redefine the runner deadline. | **EXISTS:** no model needs to remain active while idle. Runtime code consumes the subscription and starts a model invocation only for actionable work. | **PROPOSED:** consume the journal subscription directly and refetch state; raw notifications remain non-authoritative cues. | **PROPOSED:** rotate a healthy wait every 6h (`21,600/21,540s`) even where no host cap exists, then re-open immediately. |

### Codex Desktop timeout probe

**EXISTS — measured on one tested Codex Desktop build:** a direct Codex app-server `mcpServer/tool/call` invoked a local stdio MCP probe with `tool_timeout_sec=620`. The probe emitted no progress notifications, slept for approximately 605s, and returned successfully on the same request. Host- and server-observed elapsed time both rounded to approximately 605s.

This establishes that the tested Desktop build accepts a configured deadline beyond 600s and has no unconditional 600s cut-off on this path. It does not establish that every Codex build behaves identically, that 620s is a maximum, or that arbitrarily large values work.

### Host sources

- Codex configuration: [official configuration reference](https://developers.openai.com/codex/config-reference/) and [Codex configuration schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json).
- Goose bundled Developer and stdio/HTTP extension examples: [official Goose configuration guide](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/config-files.md).
- Claude Code foreground/background, hard timeout, stdio idle timeout, and progress behavior: [official MCP documentation](https://code.claude.com/docs/en/mcp) and [environment-variable reference](https://code.claude.com/docs/en/env-vars).
- Claude Desktop's approximately four-minute public baseline: [modelcontextprotocol issue 1391](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1391). Build variance and ignored configuration are also reported in [Claude Code issue 43791](https://github.com/anthropics/claude-code/issues/43791); these reports are evidence to probe, not a cross-version guarantee.
- MCP subscription/listen basis: [SEP-2575](https://modelcontextprotocol.io/seps/2575-stateless-mcp) and [Python SDK v2 subscription API](https://py.sdk.modelcontextprotocol.io/v2/api/mcp/client/subscriptions/).

## Block duration, progress, and re-arm

**PROPOSED — margin rule:** for a host or runner deadline `H` seconds, choose

```text
margin_s = min(60, max(30, ceil(0.10 * H)))
block_s  = max(1, H - margin_s)
```

A measured host profile may increase the margin; it must never decrease it below this rule without a new probe. Thus the existing Codex `230s` profile yields `200s`, Goose `300s` yields `270s`, and a one-hour Goose profile yields `3540s`. Claude Desktop deliberately raises its 240s-class margin to 40s, producing the existing safe 200s block. Codex Desktop uses the conservative capped margin: `620 - 60 = 560s`, even though the probe completed at 605s.

**PROPOSED — progress cadence:** where a host has an idle/no-progress timeout `I`, emit a lightweight MCP progress notification at

```text
progress_interval_s = min(300, floor(I / 3))
```

For Claude Code's default 1800s stdio idle timer, this is 300s. Do not emit progress merely to produce model-visible output, and do not count progress as extending the hard deadline. Profiles without a documented idle timer may remain silent.

**PROPOSED — explicit re-arm formula:** after any bounded wait completes,

```text
next_cursor = result.next_cursor if present else previously_committed_cursor
delay_s     = bounded_exponential_backoff(attempt) only for transport errors; otherwise 0
next_wait   = a2a_wait(board, cursor=next_cursor, block_s=profile.block_s)
```

Cue, normal timeout, and successful empty rotation re-arm immediately. Recoverable transport failures use jittered exponential backoff capped at 30s and refetch from the last committed cursor. Authentication, authorization, generation mismatch, or `resync_required` are state transitions, not blind retry conditions: restore credentials or obtain the indicated full snapshot/reset cursor before re-arming. A wait result must be processed before a second wait starts, so one seat has at most one live wait per board.

## Cue, authoritative refetch, and race closure

**EXISTS:** Central publishes an update to `board://<board_id>/journal` after a committed journal append. `BoardClient.events()` already reconnects and deduplicates subscription events, but the current wait bridge defaults to polling, caps a desktop wait at 200s, performs `ticket_list`/lease heartbeat work every 20s, and follows a push cue with `board_catchup(ack=False)` plus `ticket_get`. Current `board_catchup` is not a pure read: it passes through board preparation, reaps expired leases, touches caller activity, and implicitly renews the caller's held tickets. Seat-kit currently instructs Goose seats to poll every 90–120s. Worker-runtime review scans `ticket_list(status=submitted)`.

**PROPOSED — cue contract:** retain `board://<board_id>/journal` as the board-wide wake cue. Add `board://<board_id>/agent/<agent_id>` as an optional precision cue for assignments or review outcomes; it supplements rather than replaces the journal URI. A notification carries no authoritative ticket decision. On every notification or subscription reconnect, the consumer refetches committed journal state from its cursor. A normal host-timeout rotation does not refetch; it returns `timed_out=true` and re-arms from the unchanged cursor.

**PROPOSED — pure journal refetch:** add a read-only operation equivalent to

```text
journal_peek(board_id, agent_name, cursor, limit, max_events, max_bytes, expected_generation)
```

It must:

1. require `board:read` and existing board membership without joining the board;
2. apply the same caller visibility filters as `board_catchup`;
3. accept an explicit cursor only and return bounded `events`, `next_cursor`, `latest_cursor`, `has_more`, `resync_required`, `compacted_through`, `reset_cursor`, and generation/sequence metadata;
4. enforce both event-count and serialized-byte bounds; and
5. never call `prepare_board_call`, a mutation wrapper, cursor acknowledgement, activity touch, lease renewal, expiry release, or event publication.

It has no `ack` option and never reads an implicit stored cursor. The client advances its durable local cursor only after it has processed the returned page.

**PROPOSED — subscribe race closure:** a subscriber performs a pure drain from its committed cursor, opens both applicable subscriptions, then performs a second pure drain before blocking. On reconnect it repeats the pure drain before waiting. Deduplication uses board generation plus journal sequence/event identity. If a response says `resync_required`, the runner obtains the documented full state snapshot, installs `reset_cursor`, and only then resumes incremental reads. This closes the gap between snapshot and subscription without turning the notification stream into state.

## Idle seats, leases, and liveness

**PROPOSED:** an idle seat makes zero Central tool calls. It may keep a transport subscription open, but does not call a Central heartbeat, `ticket_list`, `board_catchup`, or lease operation. A seat that holds one or more claimed tickets may call only `lease_renew` while otherwise waiting. Renewal cadence is

```text
renew_every_s = min(300, floor(ticket_ttl_s / 3))
```

and must renew before `lease_expires_at - margin_s`. With the current 900s TTL this is every 300s. Ticket completion or release stops renewal before the seat returns to the idle profile.

**EXISTS:** current server-side reaping releases work on lease expiry, not merely because `last_activity_at` is old. **PROPOSED:** a silent subscriber must not be classified as dead solely because it performs no Central calls. Dashboard presence may be derived from the bridge's local subscription/connection state and labelled transport-observed, without writing to Central. If a future reaper adds agent-liveness policy, an open authenticated subscription must count as transport-live, but it must never substitute for ticket lease renewal. Lease expiry remains the authority for releasing held work.

## Model-visible metering

**PROPOSED:** dashboard push-wait efficiency is measured at the model boundary, not at the bridge-to-Central transport boundary.

Per seat, retain rolling one-hour and 24-hour counters for:

- `model_wait_returns`: number of `a2a_wait` results exposed to the model, partitioned into `cue`, `timeout`, and `error`;
- `model_wait_return_bytes`: exact UTF-8 byte length of the bounded serialized result inserted into model context; and
- derived `model_wait_returns_per_hour`, mean bytes/return, and p95 bytes/return.

Do not include subscription frames, reconnect handshakes, MCP progress messages, pure journal refetch traffic, lease renewal traffic, or bridge-to-Central request/response bytes. Existing transport byte and poll-cycle metrics may remain under a clearly separate diagnostic namespace. One cue that requires multiple pure refetch pages still counts as one model-visible return and only the final bounded tool-result bytes count toward model context.

## Rollout ownership and acceptance

| Order / implementation ticket | Files and responsibility | Dependency and acceptance |
| --- | --- | --- |
| 1/4 Central | `packages/central/src/pursers_central/central.py`; Central journal/cursor helpers and tests; `docs/coordinator-design.md` | Land first. Implement `journal_peek`, journal plus optional per-agent notifications, count/byte bounds, visibility parity, generation/reset semantics. Tests prove the operation makes no mutation, activity, acknowledgement, renewal, reap, or publish side effect. |
| 2/4 wait bridge | `tools/wait-bridge/pursers_wait_server.py`; `packages/client/src/pursers_client/client.py`; bridge/client tests; `tools/wait-bridge/WORKER-DIRECTIVE.md`; bridge README; `tools/fleet-dashboard/fleet_dashboard.py` | Depends on 1/4. Make push the default and polling an explicit fallback. Use drain-subscribe-drain, reconnect/dedup, pure refetch, host profiles, bounded return payloads, model-boundary counters, and held-ticket-only renewal. Tests prove zero idle Central calls. |
| 3/4 seat-kit | `tools/seat-kit/seat_new.py`; seat-kit tests and README; generated `board.py`, `board.sh`, `AGENTS.md`, and `.goosehints` templates | Depends on the stable 2/4 bridge API. Replace 90–120s polling instructions with one active wait and deterministic re-arm. Render distinct Goose Developer-shell and MCP-extension timeout profiles. Generated instructions must preserve one-ticket-at-a-time review/retry behavior. |
| 4/4 worker runtime | `tools/worker-runtime/pursers_worker.py`; worker-runtime tests, README, and runtime-owned profile/config schema | Depends on the stable 2/4 bridge API; may proceed in parallel with 3/4. Replace worker and reviewer `ticket_list` polling with subscription cue plus pure refetch, while keeping lease renewal independent. Headless workers wait without a model turn and invoke the model only for actionable work. |

The rollout is complete only when: push is the default; poll is an explicit compatibility fallback; idle seats generate zero Central calls; held tickets renew independently of journal reads; every model-visible cue/timeout/error has exactly one bounded return followed by one re-arm; reconnect and compaction tests show no lost or duplicated actionable event; model metrics exclude bridge transport bytes; and no implementation depends on MCP Tasks.
