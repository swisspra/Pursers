# Projection recovery on the private 5.2.1 fork

## Problem and repair boundary

Central's ticket list is constrained by response bytes as well as row count.
It can return a short page with a cursor, or a compact `oversized_for_list`
record without `tier`. Counting ticket IDs did not establish usable operational
data. The resident treated such data as complete and exited on tier validation.
The installed resident also imported coordinator code from a separate 5.0.8
clone, so replacing only the exported coordinator would miss its actual reader.

The VM's projection v3 and approval-audit v3 patches were applied as scoped
patches to 5.2.1, preserving the rest of the newer coordinator. Additional
consumer integration closes the inventory and fleet-effect gaps left in the
staged repair. No stored tiers, memberships or ticket holds are rewritten.

## Read and effect contracts

- Operational lists explicitly request work view. Cursor traversal checks
  counts, filters, unique IDs and a stable watermark. Byte-heavy pages are
  retried with a smaller row limit before detail hydration. Both board reads
  share a 100-list-request limit, 32-detail-call limit, 750,000-detail-byte limit
  and ten-second deadline. Exhaustion or inconsistent input remains incomplete.
- Missing operational fields are recovered with the same reader and board.
  A present invalid tier is never replaced with a default.
- An incomplete active registry board blocks fleet effects before cap commands,
  local recovery, supervisor operations or executor operations. Fresh inventory
  cannot erase an earlier incomplete flag. Malformed omission metadata remains
  unknown. Exact or unknown report-writing holders remain protected.
- Incomplete control-plane evidence blocks new source intake scheduling and
  paid intake decisions. Existing configured ceilings, provider policy and
  authorization grants remain authoritative.
- Approval inputs are cursor-paged and hydrated separately from operational
  work fields. A failed refresh invalidates complete cached results. Each
  approval is checked against its registered repository and pinned revision;
  one shared board cannot substitute its control repository for another project.

## Agent payloads and tokens

Direct Central and the model-facing relay default `ticket_list` to
`view="summary", limit=10`, with a 32 KiB summary-page budget. `max_bytes` can
select a page budget between 8 KiB and 192 KiB; this is a byte bound, not a model
token count. Explicit work/full pages keep the existing 192 KiB default.
`board_snapshot` defaults to `limit=10, max_bytes=16384` and reports all omitted
counts. It is an overview, not a pageable ticket-list replacement.

Callers follow `next_cursor` unchanged with the same filters and view when the
task needs more matching tickets; `has_more=false` identifies the last page.
The protocol result and server instructions point to `ticket_get(view="full")`
for one exact ticket and `ticket_history_list` for retained evidence. All relay
profiles, including worker and reviewer, expose the history tool. A summary is
not evidence and a partial page cannot establish board-wide absence.

The internal SDK explicitly requests work view, preserving operational consumers.
Butler and coordinator retain their bounded complete cursor readers. Exact detail
reads restore retained history overflow, including annotations outside the inline
window. An individual history entry too large for a normal page has an explicit
oversized marker; a requested full ticket can recover its retained value. Existing
retention gaps remain disclosed; this repair cannot invent previously lost data.
Stored text, ticket capacity and board-state capacity are unchanged.

Read-only measurements on the existing control board returned 191,339 JSON
bytes for the old ticket-list defaults versus 20,734 for the model-facing
summary defaults. Snapshot payloads were 275,770 versus 16,183 bytes. These
are measured JSON sizes, not tokenizer counts or guaranteed billing savings.

### Direct Central readers and paid model inputs

The internal Butler and coordinator readers retain complete operational evidence;
the relay's ten-row and 16 KB defaults do not apply to them. Structured board state
retains its expanded 262,144-character capacity, ticket descriptions retain their
5,000-character capacity, and individual worker/reviewer reads retain their evidence.

The coordinator daemon uses deterministic intake drafting and makes no provider
requests. Butler's intake decision uses source counts, queue counts and deduplicated
fleet capacity, rather than serialized tickets. Its question drafter selects only
the question, kind, verdict, policy rule, cited evidence and fallback response.
Grouping sends candidate rule/file/message buckets and occurrence indices/lines;
descriptions, ticket histories and unrelated Sonar response fields stay local.
An unchanged grouping snapshot reuses its private plan, and zero open source issues
skip the paid intake decision.

Question drafting now checks its existing 12,000-byte input budget for both provider
protocols, including `pursers_json_v1`. Intake and grouping retain their existing
12,000-byte and 32,000-byte input budgets. Serialized model input preserves literal
Unicode rather than presenting JSON code-point escapes to the model. Oversized
input is rejected before provider I/O; evidence is not silently cut. These are
model input bounds, not ticket or board-state storage limits.

The SDK's standard pagination applies to `tools/list`, `resources/list`,
`resources/templates/list` and `prompts/list`. Tickets live inside a tool result,
so Pursers reuses its existing application-level cursor rather than migrating
tickets to a new resource catalog. Neither SDK nor IDE is assumed to drain every
page automatically. Internal readers implement the loop; model clients receive
explicit continuation metadata and read only what their current task requires.

## Validation

The original staged tests were first exercised against the unchanged reader.
Additional regressions cover cursor traversal, byte-heavy rows, incomplete
inventory, malformed metadata, report-writing holders and intake cost fences.

The two-route integration test uses a disposable real Central instance and the
real MCP client protocol. One route consumes a synthetic Sonar connector issue
through the source poller; the other accepts a human-approved coordinator ask.
Both create an intake ticket using the narrow intake credential scope, provoke
a real byte-cap stub and recover its original tier into fleet demand. The
external Sonar tool is synthetic in this test; live connector reads are a
separate deployment check. No customer ticket is created by the integration test.

Before cutover, run the complete fourteen-suite gate, the release/component
lock check, integration manifest check and secret scan on one frozen commit.
Run the patched reader with the VM's interpreter against the current registry
without joining or modifying a seat. Both operational and intake projections
must be complete for the intended active boards.

## Deployment and rollback

Create a Git checkout of the tested commit in a separate release directory and
build the matching private package cohort in its own virtualenv. Keep the
existing stable runtime prefix as a compatibility link so executor template
command strings and signed policy bindings do not change. Set Butler's `--repo`
to the pinned service source, which now contains Git metadata and the 5.2.1
coordinator. Project work directories remain separately registered.

Before switching, record active work/review leases, unit/configuration hashes,
Git SHA and effective authorization. Back up SQLite consistently, along with
the executor state, source index, cursors, service definitions and private
configuration. Keep the previous source/virtualenv prefix for rollback.
Do not preempt a lease holder; use a lease-free cutover or wait for work to drain.

After switching, verify the loaded source and imported coordinator paths, exact
commit, service health, repeated successful refreshes, complete project reads
and unchanged configuration/grants. Check live Sonar tools using the existing
connector credentials. Existing `needs_human` work remains held; source recovery
does not decide application requirements or authorize environment merges.

For a code rollback, stop the new control-plane services, restore the previous
runtime link and saved unit, reload the service manager and restart the prior
cohort. Code rollback does not imply database rollback. Preserve all newer
state and receipts; use a state restore only for a separately diagnosed need.
