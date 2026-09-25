# Ticket progress checkpoint contract

Status: design for a future additive feature; no runtime behavior changes here.

This contract adds a worker-authored estimate of how much of the current work
attempt is complete. It is evidence, not a timer. Automatic lease maintenance
never creates, refreshes, raises, or clears the estimate. Workflow status,
lease health, and estimated progress remain three separate facts.

## Goals and non-goals

The record answers: “What bounded range did the current worker last assess,
why, how confident were they, who assessed it, and is that assessment fresh?”
It does not predict a delivery date, measure model activity, infer progress from
elapsed time, or replace claim, submission, review, and rejection states.

Required properties:

- absence means **not assessed**, never zero percent;
- every value is attributed to the authenticated current worker and server time;
- a short evidence statement is required and scrubbed;
- ranges may move backward or widen when new work is discovered;
- freshness expires independently of a live lease;
- each attempt starts unknown and keeps prior estimates only in bounded audit
  history; and
- private UI, audit events, and any public aggregate use different projections.

## Source-backed baseline in 5.0.5

The current source has no canonical ticket-progress record:

- `ticket_update` accepts tier, skills, preferred/excluded agents, and parking,
  not worker progress
  (`packages/central/src/pursers_central/central.py:9246-9270`).
- `ticket_annotate` is free text and is authorized for a coordinator/admin or a
  reviewer note, not the current worker
  (`packages/central/src/pursers_central/central.py:9408-9453`).
- `renew_claim` changes TTL, expiry, renewal time, and renewal-source metadata
  only (`packages/central/src/pursers_central/central.py:3502-3524`). The public
  `lease_renew` tool delegates to it and returns lease facts only
  (`packages/central/src/pursers_central/central.py:11000-11089`).
- keepalive and model renewal are already distinguishable: a keepalive begins a
  `lease_keepalive_only_since` interval, while a model renewal clears it
  (`packages/central/src/pursers_central/central.py:3502-3514`).
- the client mirrors these boundaries with distinct `ticket_update`,
  `ticket_annotate`, and `lease_renew` calls
  (`packages/client/src/pursers_client/client.py:1098-1141`,
  `packages/client/src/pursers_client/client.py:1321-1330`).
- the Fleet private projection already carries claim age, renewal source,
  keepalive-only age, and TTL separately
  (`tools/fleet-dashboard/fleet_dashboard.py:3149-3169`), and warns when
  keepalive is the only recent signal
  (`tools/fleet-dashboard/fleet_dashboard.py:8360-8375`).
- rejection of a generated ticket reopens it and removes its current claim and
  submission ownership fields
  (`packages/central/src/pursers_central/central.py:11758-11849`).

These boundaries are useful: the new mechanism should extend them rather than
reinterpret `updated_at`, annotations, memory, or lease renewal.

## Mechanisms considered

### A. Structured text in annotations or progress memory

The smallest apparent change is a conventional annotation or a
`memory_type=progress` document. It reuses storage and journal plumbing, but is
not a safe current-state contract. Workers cannot create ticket annotations
today; free text requires every reader to parse and select the latest valid
shape; memory is not an atomic child of a ticket attempt; staleness and reset
would be consumer-specific; and history truncation could change which estimate
appears current. This is acceptable for narrative notes, not UI state.

### B. Add progress arguments to `lease_renew`

This would make the lease holder check convenient, but it violates the primary
boundary. Wait bridges renew leases without a model assessment. Optional
progress fields invite runtimes to copy the old percent during keepalive,
silently refreshing its timestamp and making stale work look active. Reject
this mechanism even if the server promises not to update absent fields: the
combined API remains too easy to misuse and audit.

### C. Dedicated `ticket_progress_update` tool — recommended

A narrow tool can require the current work lease, validate one exact schema,
write current state plus bounded history atomically, and emit a typed event. It
does not touch lease fields or workflow status. The extra tool is a small
surface cost and gives clients, audit consumers, and tests an unambiguous
contract. This is the recommended mechanism.

## Canonical private schema

An active ticket may have one `progress` object and a bounded
`progress_history`. The current record is exactly:

```json
{
  "schema_version": 1,
  "attempt": 2,
  "revision": 3,
  "low_percent": 35,
  "high_percent": 55,
  "confidence": "medium",
  "evidence": "Parser is complete; integration and focused tests remain.",
  "assessment_source": "model_checkpoint",
  "assessed_at": "2030-01-02T03:04:05Z",
  "fresh_until": "2030-01-02T03:49:05Z",
  "assessed_by": {
    "agent_id": "AI-SYNTHETIC",
    "agent_name": "worker-example",
    "principal_id": "PR-SYNTHETIC"
  }
}
```

Normative rules:

- `schema_version` is `1`; `attempt` and `revision` are positive integers.
- `low_percent` and `high_percent` are integers from 0 through 99, with low no
  greater than high. One number is represented by equal bounds. One hundred is
  workflow completion and is shown from submitted/closed status, not asserted
  as an in-progress estimate.
- `confidence` is `low`, `medium`, or `high`. It describes confidence that the
  remaining scope is understood, not the probability of successful review.
- `evidence` is required, 1–280 characters after trimming, and passes the
  board scrub profile. It states completed evidence and material remaining
  work; it contains no secrets, paths, identifiers, logs, or raw model thought.
- the server supplies source, timestamps, revision, attempt, and authenticated
  attribution. A caller cannot override them.
- `fresh_until` is fixed when written as `assessed_at + clamp(3 * claim_ttl_s,
  900, 10800)`. Later lease renewal or TTL changes do not move it.
- the API returns `fresh`, `stale`, or `unknown` as a derived projection. It
  does not persist a mutable stale boolean.

`progress_history` stores the same record plus `ended_at` and `end_reason` when
superseded or reset. It uses the board's bounded-history policy (default last
50) and a `progress_history_omitted_count`; add it to the existing bounded
history set. The current record is copied to history before replacement, in
the same board transaction. History is private and excluded from compact list
views by default.

## Tool and authorization contract

Add this server and client method:

```text
ticket_progress_update(
  board_id,
  agent_name,
  ticket_id,
  low_percent,
  high_percent,
  confidence,
  evidence,
  expected_revision,
  expected_generation?
)
```

`expected_revision` is `0` only when no current record exists. Otherwise it
must equal the current revision; conflicts return the current revision without
changing state. This prevents an older model turn from overwriting a newer
checkpoint.

The server requires `board:write`, an active member, a live pre-submission
ticket, and exact matches for both `claimed_by_agent_id` and
`claimed_by_principal_id`. Coordinator authority does not impersonate the
worker. The tool calls the ordinary reap path before validation so an expired
lease cannot write progress. It uses the existing scrub profile and records
scrub audit counts. A byte-for-byte semantic duplicate is rejected as a no-op;
the tool never renews the lease, changes `updated_at` used for workflow timing,
or changes ticket status. It may set a dedicated `progress_updated_at`.

The compact mutation receipt contains only:

```json
{
  "ok": true,
  "ticket_id": "TK-SYNTHETIC",
  "attempt": 2,
  "revision": 3,
  "fresh_until": "2030-01-02T03:49:05Z",
  "at": "2030-01-02T03:04:05Z"
}
```

No API automatically calls this tool. Seat prompts should request a checkpoint
after material evidence changes—for example a design decision, implementation
milestone, focused-test result, or newly discovered scope—not on a timer and
not at every tool call. A runtime may remind the model that an assessment is
stale, but only a subsequent model turn chooses and submits new bounds and
evidence.

## Attempt and workflow transitions

Central owns an integer `work_attempt`. The first successful claim sets it to
1. A claim after explicit unclaim, lease expiry, human-input handoff that ends
ownership, or review rejection increments it. Claims that merely move
`claimed` to `in_progress`, and lease renewals, do not.

| Transition | Current progress behavior | Private UI behavior |
| --- | --- | --- |
| first claim or re-claim | no current record; retain prior history | “Progress not assessed” |
| explicit model checkpoint | replace current after archiving prior revision | show range, confidence, assessor, age and evidence |
| lease keepalive/model renewal | no progress mutation of any kind | lease badge may change; estimate and age do not |
| estimate passes `fresh_until` | record remains; derived state becomes stale | show old range muted with “Stale assessment” |
| request human input without releasing claim | retain record; freshness continues | show waiting state and last assessment separately |
| explicit unclaim or lease expiry | archive current with reason; clear current | open/unclaimed, no current estimate |
| submit | archive current as `submitted`; clear current | workflow badge says “Submitted”; never synthesize 100% |
| review claim/release | no current estimate exists | review state only |
| approve/close | no current estimate; history retained | “Complete” comes from closed status |
| reject/reopen | archive defensively if present; increment next attempt; clear | “Rework not assessed” until new worker checkpoint |
| cancel/terminate | archive and clear | terminal status only |

A server-owned reset emits an audit event even when no current progress exists,
but does not manufacture a revision. Carrying an estimate across attempts is
forbidden: rejected scope or a new worker invalidates its denominator.

## Audit events and read projections

An explicit checkpoint emits `ticket_progress_updated`. The journal allowlist
contains ticket ID, schema version, attempt, revision, low/high percent,
confidence, `assessed_at`, `fresh_until`, assessor agent/principal IDs, and a
`progress_ref` such as the ticket resource plus attempt and revision. It does
**not** copy evidence text into the journal; authorized readers resolve the
ticket resource.

Lifecycle clearing emits `ticket_progress_reset` with ticket ID, attempt,
previous revision, reset reason (`unclaimed`, `lease_expired`, `submitted`,
`review_rejected`, `canceled`, or `terminated`), actor, and timestamp. These
events join the known/core event sets so wait-bridge consumers do not treat
them as unknown. They are audit notifications, not offer or claim cues.

`ticket_get(view="work"|"full")` includes current progress and freshness.
Summary/list projection includes only range, confidence, derived freshness,
and assessed time; it omits evidence and assessor identities. Archived ticket
indexes omit progress entirely; an authorized full archive read may retain
bounded history. Fleet's private detail projection may add the current safe
fields without folding them into lease status.

## Private UI

In Work and ticket detail, render progress only for active claimed states:

- unknown: “Progress not assessed” with no empty bar and no `0%`;
- fresh point estimate: “About 40% · medium confidence”;
- fresh range: “35–55% · medium confidence”;
- stale: the same value muted, followed by “Stale · assessed 52 min ago”; and
- submitted/review/terminal: show workflow state, not a percent.

The expanded detail shows the short evidence and private assessor label. The
list row shows only range/confidence/freshness. A separate lease line continues
to show active, expiring, or keepalive-only state. Never animate progress
between checkpoints, extrapolate from time, or let a renewed lease move the
bar. Sort/filter must not interpret unknown as zero.

## Public projection

Public mode never returns a ticket-level progress value, evidence, confidence,
assessor, attempt, revision, or timestamp. Aggregate progress is separately
operator-enabled and uses this exact, all-or-nothing algorithm with fixed
minimum cell size `k = 5`:

1. At the end of each UTC-aligned publication window (at least 15 minutes),
   freeze the project cohort as every ticket in an active claimed state at that
   instant. Use only that completed snapshot after the next window ends, so the
   public data is delayed by at least one full window.
2. The denominator `N` is **all** tickets in that frozen cohort, including
   tickets with unknown, absent, invalid, or stale progress. If `N < 5`, omit
   the entire progress object.
3. Assign each ticket to exactly one of four cells. A record fresh at the frozen
   instant uses its range midpoint: `early` 0–24, `middle` 25–74, or `late`
   75–99. Every other ticket goes to `unassessed`, including unknown/absent
   records and records whose `fresh_until` is at or before the snapshot time.
4. A zero-count cell is safe and does not trigger suppression. If **any
   positive** cell has count 1–4, omit the entire object. There is no partial or
   complementary-cell publication: suppressing one share would expose it from
   the other shares' 100% complement.
5. Jointly round the four shares with largest remainder. For cells ordered
   `early`, `middle`, `late`, `unassessed`, compute quota units
   `q[i] = 4 * count[i] / N`, assign `floor(q[i])`, then give the remaining
   `4 - sum(floor(q))` units to the largest fractional remainders. Break exact
   ties by that fixed cell order. Multiply units by 25%. The four published
   shares always total exactly 100%; zero cells remain 0%.
6. Set `cohort` to `several` for `5 <= N <= 19`, or `many` for `N >= 20`.
   Publish only `cohort`, `early_share`, `middle_share`, `late_share`,
   `unassessed_share` (`0%|25%|50%|75%|100%`), and `freshness: "delayed"`.
   Publish no counts or window timestamp.

Examples are normative. Counts `(early,middle,late,unassessed) = (10,0,0,0)`
publish `several` and `(100%,0%,0%,0%)`: zero cells are safe. `(5,5,5,0)`
publish `several` and `(50%,25%,25%,0%)`; equal one-third remainders award the
extra unit to `early`. `(5,9,6,0)` publish `many` and `(25%,50%,25%,0%)`.
`(5,0,0,5)` publish `several` and `(50%,0%,0%,50%)`, proving stale/unknown
tickets stay in the denominator. `(5,4,0,0)` is wholly suppressed because a
positive cell is below `k`. Cohort sizes 4, 5, 19, and 20 respectively produce
suppressed, `several`, `several`, and `many`, subject to the positive-cell rule.

Do not publish confidence: small combinations of band and confidence increase
re-identification risk. The public response uses anonymous project aliases only
and cannot be joined to private ticket rows.

## Compatibility and rollout

This is additive schema version 1:

1. Central accepts old tickets without `work_attempt`, `progress`, or history;
   projection treats them as unknown. On the next claim, initialize attempt 1.
2. Add event allowlists, mutation logic, resource projection, and server tests
   behind a board capability `ticket_progress_v1=false` by default.
3. Add the optional `BoardClient.ticket_progress_update` method. A new client
   connected to an old server receives method-not-found and continues without
   progress; it must not emulate the call with lease renew or annotations.
4. Teach wait bridge and Fleet to advertise/read the capability. Additive ticket
   fields are optional; old clients and wait bridges must be verified to ignore
   the two new event kinds rather than fail or wake as if they were offers.
   Servers must keep compact responses bounded.
5. Enable on a synthetic board, then selected private boards. Public aggregates
   remain separately disabled until their suppression tests pass.

No backfill is performed from annotations, memories, elapsed claim time, git
history, or lease timestamps. Those sources cannot prove a model assessment.
Rollback disables the tool and UI write control; Central continues to preserve
and project stored version-1 records read-only. A later cleanup may archive the
optional fields only after the retention window. Never rewrite them into lease
metadata.

## Acceptance and negative tests

1. **Schema:** boundaries 0, 1, 98, 99; equal bounds; low greater than high;
   booleans/floats; confidence enum; blank/281-character evidence; scrub rejects.
2. **Authorization:** non-holder, same name/different agent, same agent/different
   principal, expired lease, open, submitted, reviewing, closed, and canceled
   tickets cannot write. Current exact holder in each pre-submission state can.
3. **Optimistic concurrency:** two updates with one expected revision produce
   one success and one conflict; retry must refetch rather than overwrite.
4. **Lease separation:** snapshot the progress record and event count, run model
   renewal, keepalive renewal, implicit touch, board reap, and repeated waits;
   progress bytes, revision, `assessed_at`, and `fresh_until` remain identical.
5. **Freshness:** use an injected clock at the boundary before/equal/after
   `fresh_until`; renewal and TTL configuration changes do not postpone it.
6. **Lifecycle table:** exercise claim, in-progress, human wait, unclaim,
   expiry, submit, review claim/release, approve, reject/reopen, cancel, and
   terminate. Assert exact archive reason, clearing, and attempt number.
7. **History bounds:** exceed the configured limit, verify omitted count and
   audit references, and prove truncation cannot alter current state.
8. **Audit:** explicit updates and every reset emit exactly one allowlisted
   event; evidence text and unrelated ticket fields never enter the journal.
9. **Client/version:** old-client/new-server, new-client/old-server, feature off,
   unknown additive fields, reconnect, duplicate retry, and compact receipts.
10. **Private UI:** product-produced states render unknown without `0%`, fresh
    point/range, stale, rework unknown, and workflow completion. Lease-only
    updates never move or animate the estimate.
11. **Public privacy:** safe zero cells; 5/5/5 fixed-order tie; unequal 5/9/6
    cells; unknown/stale 5/5 remainder; a positive cell below `k` suppresses the
    whole object; sizes 4/5 and 19/20 cross the publication and cohort-label
    boundaries; joint 25% rounding totals exactly 100%; delay and alias-only
    output; no evidence, assessor, ticket ID, timestamps, counts, or confidence.
12. **Property tests:** arbitrary valid update sequences preserve range bounds,
    strictly increasing revisions per attempt, no cross-attempt current record,
    bounded history, and zero progress mutations from lease-only operations.

Release requires Central, client, wait-bridge, Fleet projection/UI, generated
tool-reference, compatibility, and public-projection suites to pass together.
This document authorizes none of those runtime changes.
