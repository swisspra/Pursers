# Controlled sequential-versus-parallel case study protocol

Status: preregistration template; no benchmark results are claimed here.

This protocol measures the same finite ticket set under sequential and parallel
execution while holding the work, models, provider, host class, and acceptance
gates constant. Its output is an external case study, not a product benchmark
endpoint. Collectors may read existing board records, but they must not change
Pursers runtime behavior or publish operational identifiers.

## Claim boundary

The study may support only this claim:

> For the preregistered ticket set and environment, parallel scheduling changed
> accepted-ticket throughput and completion time by the reported amount.

It does not establish universal model quality, cost, or scaling. A result is
publishable only when both arms complete under the rules below. A stopped,
partially observed, or unmatched arm remains an incident record, not a speedup
claim. No estimate may be copied from a dry run, simulated from expected
values, or selected because it looks favorable.

## 1. Preregister the experiment

Create a private, append-only study manifest before the first measured action.
Hash it with SHA-256 and include the digest in every run record. The manifest
must contain:

- study and pair identifiers; protocol revision and candidate commit;
- the complete ordered ticket template list and a digest of every task body,
  fixture, starting repository tree, and acceptance command;
- arm labels, the randomized arm order for each pair, and the random seed;
- provider, exact model identifier, model settings, tool policy, context
  bootstrap, seat-to-ticket mapping, and retry limits;
- host class, OS/runtime versions, logical CPUs, memory, storage class, network
  class, provider/account quota, configured concurrency, and the declared
  concurrency ceiling;
- test and independent-review gates, stopping rules, exclusion rules, timeout,
  warm-up policy, repetition count, primary metric, and uncertainty method;
- collector revision, wall-clock source, monotonic-clock source, and clock-sync
  check; and
- the private location of source records and the public alias namespace.

Use at least two counterbalanced pairs when resources allow: randomize each
pair to sequential-then-parallel or parallel-then-sequential, with balanced
`AB`/`BA` order. More repetitions improve uncertainty estimates; the count
must be fixed before measurement. A single pair may be shown as a transparent
demonstration but must not be described as a stable estimate.

### Finite ticket set

Freeze tickets before assignment. Each arm receives byte-identical title,
description, scope, related inputs, acceptance commands, and starting commit.
Ticket dependencies must be encoded as a preregistered DAG. A ticket becomes
eligible only after its declared predecessors pass; undeclared dependencies
invalidate the pair. Generate fresh board tickets and clean worktrees for each
arm so outputs from the first arm cannot seed the second.

Assign every ticket a fixed execution profile. In both arms, the same profile
uses the same provider, model, settings, prompt/bootstrap, and tools. If the
parallel arm has several seats, the sequential arm uses the same seat profiles
and preregistered ticket-to-profile mapping, but releases at most one eligible
ticket at a time. This balances seat/model effects while changing only allowed
concurrency.

### Arms

- **Sequential:** one active work lease across the study. Review begins under
  the same preregistered policy; if review is part of the measured pipeline,
  its concurrency is also one.
- **Parallel:** at most `C` concurrent work leases, where `C` is fixed in the
  manifest and cannot exceed the measured host/provider ceiling. Review uses
  the preregistered parallel limit.

Both arms use the same independent reviewers, review rubric, acceptance
commands, retry policy, and terminal definition. Reviewers must not know the
aggregate timing result while deciding verdicts.

## 2. Control the environment

Run paired arms on isolated but equivalently provisioned hosts, or alternate
arms on the same quiesced host. Record which design was used. Before each arm:

1. Restore the pinned repository commit and fixture snapshot; prove the tree is
   clean and record their digests.
2. Reset only study-owned caches according to the preregistered warm/cold-cache
   policy. Use the same policy for both arms.
3. Verify provider/model availability, quota, network class, free disk, memory,
   CPU count, load, and clock synchronization.
4. Start the external sampler and record setup start, ready, and release times
   with both UTC wall time and a monotonic offset.
5. Admit exactly the frozen tickets, then release the arm only after every
   required seat and reviewer reports ready.

Do not run unrelated jobs on a study host. Abort the pair on model substitution,
provider/account change, host-class drift, fixture drift, gate drift, clock
step, collector loss, or an undeclared dependency. Preserve the failed pair and
its reason; never silently rerun it. A replacement is a new, fully identified
pair and both attempts appear in the accounting.

## 3. Event and timestamp record

Store timestamps as RFC 3339 UTC with source-clock metadata. Store durations as
integer monotonic nanoseconds. Wall time orders events across processes;
monotonic time measures intervals within one collector epoch. Never subtract
wall timestamps across a detected clock step.

For each arm, capture these study events:

| Event | Definition |
| --- | --- |
| `setup_started` | First arm-specific provisioning action. |
| `environment_ready` | All pinned inputs, seats, reviewers, samplers, and gates verified. |
| `arm_released` | Scheduler is allowed to expose the frozen ticket set. |
| `ticket_eligible` | Ticket exists and all preregistered dependencies pass. |
| `ticket_offered` | First valid work offer after eligibility; retain later offer attempts too. |
| `ticket_claimed` | Each accepted work claim, including post-rejection attempts. |
| `ticket_submitted` | Each submission paired with its claim and exact candidate SHA. |
| `review_started` | Reviewer actually begins evaluation, not merely receives an offer. |
| `review_verdict` | Every approve/reject verdict with gate outcome and rework link. |
| `ticket_accepted` | Independent approval and all acceptance gates pass. |
| `arm_finished` | Every ticket is accepted or has reached a declared terminal failure. |

Every record also carries the manifest digest, pair, arm, sanitized ticket and
seat aliases, attempt number, event source, source sequence/watermark, collector
epoch, UTC timestamp, monotonic offset when available, and an explicit
`observed`, `derived`, or `missing` quality label. Preserve duplicate raw events;
deduplicate only by a documented source event key in the derived table.

### Existing board evidence

Current board state and journal data can provide much of the lifecycle:

- ticket creation stores `created_at`, and emits `ticket_created`
  (`packages/central/src/pursers_central/central.py:9144-9227`);
- work dispatch retains offered time in `dispatch_history`, and projected
  journal events expose offer kind, sanitized recipient name, expiry, reason,
  and `occurred_at` (`packages/central/src/pursers_central/central.py:354-382`);
- claims store `claimed_at` and emit an open-to-claimed status transition
  (`packages/central/src/pursers_central/central.py:9894-9991`);
- submissions store `submitted_at`, submission history, file list, and verified
  remote-tip preflight when required, then emit a transition to `submitted`
  (`packages/central/src/pursers_central/central.py:11142-11353`);
- reviews store `reviewed_at`, verdict, rejection count, and review history,
  then emit the terminal or reopen transition
  (`packages/central/src/pursers_central/central.py:11751-11935`); and
- the Fleet projection already assembles created, offered, claimed, submitted,
  and reviewed stages from snapshot plus ordered journal events
  (`tools/fleet-dashboard/fleet_dashboard.py:2820-2965`).

Fetch a bounded ticket snapshot plus the journal range covering the arm. Record
the start/end watermarks and any truncation or resync indicator. Snapshot fields
are fallback state; journal sequence is the ordering source. If the necessary
journal range is truncated, mark the affected timing unavailable rather than
reconstructing it from guesses.

### Missing telemetry

The existing board records do **not** establish all study facts. The external
collector must capture:

- monotonic setup, ready, release, eligibility, review-start, and gate timing;
- actual model/provider/version/settings used per attempt and any substitution;
- provider queueing, rate-limit responses, retries, token usage, and request
  latency where the provider exposes them;
- host CPU, memory pressure, disk and network utilization, process overlap,
  and unrelated load;
- time spent actively editing/reasoning versus waiting on tools, provider,
  tests, review, or dependencies;
- exact focused/full gate start, finish, exit code, tested SHA, and output
  digest; and
- reason-coded infrastructure, worker, test, review, timeout, cancellation,
  and collector failures.

`claimed_at` to `submitted_at` is therefore elapsed ownership time, not human
or model active time. `reviewed_at` minus `submitted_at` combines reviewer queue
and review work unless `review_started` is externally recorded. The Fleet
runtime evidence channel documents causal product-action observations, not a
general benchmark clock; do not reinterpret its opt-in records as missing
provider or host telemetry
(`tools/fleet-dashboard/EVIDENCE_TRACE_CONTRACT.md:1-174`).

## 4. Derive the metrics

Let `e_i`, `c_ij`, `s_ij`, `r_ij`, and `v_ij` be ticket eligibility, claim,
submission, review-start, and verdict times for ticket `i`, attempt `j`.
Let `R` be arm release and `F` arm finish. All differences use the monotonic
timeline where available.

- Setup time: `environment_ready - setup_started`.
- Initial queue time: first claim minus `ticket_eligible`.
- Offer delay: first valid offer minus `ticket_eligible`.
- Attempt ownership time: `s_ij - c_ij`.
- Review queue time: `r_ij - s_ij`; unavailable without `review_started`.
- Review work time: `v_ij - r_ij`; unavailable without `review_started`.
- Review elapsed time: `v_ij - s_ij`, always labeled as combined queue/work.
- Ticket flow time: accepted verdict minus `ticket_eligible`.
- Execution makespan: `F - R`.
- End-to-end wall time: `F - setup_started`.
- Accepted throughput: accepted tickets divided by execution makespan in hours.
- Rework: rejected submissions per accepted ticket, plus added ownership and
  review elapsed time on all later attempts.

Report ticket count, accepted count, terminal failures, abandonments, timeouts,
provider/tool/test failures, rejection cycles, and total attempts beside every
time or throughput result. Report per-ticket medians and interquartile ranges;
retain maxima so stragglers are visible. Never sum overlapping per-ticket wall
times and label the result as arm wall time.

For paired run `k`, define:

```text
speedup_k = sequential_execution_makespan_k / parallel_execution_makespan_k
efficiency_k = speedup_k / C
throughput_ratio_k = parallel_accepted_throughput_k / sequential_accepted_throughput_k
```

The primary estimate is the median paired log speedup transformed back with
`exp(median(log(speedup_k)))`. Report all pair values and a two-sided 95%
percentile confidence interval from a preregistered paired bootstrap (resample
pairs, fixed seed, at least 10,000 draws). With fewer than five valid pairs,
show the interval as descriptive and explicitly warn that it is unstable. Do
not replace failed pairs with successful ones, drop warm/cold outliers after
viewing outcomes, or switch between mean and median after collection.

### Concurrency ceiling

The manifest declares `C_configured` as the lowest applicable cap across work
seats, review seats, provider request quota, host CPU/memory capacity, and any
dependency width in the ticket DAG. Derive `C_observed` from overlapping live
claim intervals, and separately report peak provider requests and peak gate
processes. If `C_observed < C_configured`, report the binding reason; do not
describe the configured number as achieved parallelism. Include utilization
and saturation plots so a speedup plateau is not mistaken for scheduler cost.

## 5. Privacy and publication

Raw capture stays private. It may contain source board, ticket, event, agent,
principal, run, host, branch, commit, and provider-request identifiers. Store
the raw records and alias map in a study-owned directory readable only by the
collector owner. Never place tokens, request bodies, prompts, review notes,
home paths, hostnames, or repository remotes in either public or private metric
tables unless separately required and access-controlled.

Before analysis, generate stable per-study aliases (`board-A`, `ticket-01`,
`seat-01`, `host-A`, `run-01`). Keep the source-to-alias map private. A suitable
map key is `HMAC-SHA-256(study_secret, type || NUL || source_id)`; neither the
secret, digest, nor source ID is published. The public dataset contains only
the readable aliases, preregistered task categories, rounded durations,
aggregate resource statistics, gate outcomes, and failure categories. Suppress
free text and any cell whose rare combination could re-identify a source.

Run the repository leak scanner against every public report, dataset, image
metadata dump, subtitle file, and storyboard export. Manually inspect frames
for terminal chrome, browser history, paths, branches, notifications, and
unrelated project labels. Publish the manifest digest and collector/analyzer
source digest so the aggregate can be reproduced privately without exposing
the source IDs.

## 6. Dashboard image and video storyboard

Use only the study aliases above. The storyboard is a plan for later capture;
this ticket does not authorize production or publication.

1. **Control card (still, 5 s):** protocol revision, candidate digest prefix,
   finite ticket count, provider/model alias, host class, `C`, repetitions, and
   matched-gate badges. Caption: “Only scheduling concurrency changes.”
2. **Two-lane release (8 s):** synchronized `run-01/S` and `run-01/P` lanes.
   Identical ticket cards appear at release; the sequential lane admits one,
   while parallel admits up to `C`. Use event timestamps, not staged motion, to
   position cards.
3. **Lifecycle detail (8 s):** one aliased ticket expands into eligible, queue,
   ownership, review queue/work, rejection, and accepted segments. Unknown
   review-start telemetry is hatched and labeled “combined/unavailable.”
4. **Concurrency and ceiling (8 s):** actual overlapping claims, provider
   requests, CPU, and memory share one time axis with horizontal configured and
   observed ceilings. Annotate the binding ceiling.
5. **Outcome (8 s):** paired makespan dumbbell plot, accepted throughput,
   speedup and bootstrap interval, plus attempts/rework/failure counts. Show all
   runs; failed or invalid pairs remain visible with reasons.
6. **Limits and provenance (still, 5 s):** manifest and analyzer digest prefixes,
   clock/telemetry coverage, missing fields, exclusions fixed before collection,
   and “applies to this ticket set and environment.”

Images use the same six frames at a legible static breakpoint. Video includes
captions and a reduced-motion cut using cross-fades only. No frame may display
real board names, ticket IDs, seat names, project names, paths, hosts, tokens,
or notification content.

## 7. Analysis and release checklist

The independent analyst/reviewer receives the frozen manifest, raw private
records, alias map, derivation code, public artifacts, and gate outputs. They
must verify:

- both arms match the manifest and exact ticket/input/gate digests;
- arm order and inclusion follow the preregistered seed and rules;
- event sequence and snapshot watermarks are complete, with missing fields
  labeled rather than fabricated;
- every accepted ticket has a passing gate and independent approval;
- durations recompute from source events, overlapping work is not double-counted,
  and failed/reworked attempts remain in totals;
- formulas, bootstrap seed/draw count, concurrency limits, rounding, and public
  aggregates reproduce from the private sanitized analysis table;
- leak scans pass and a human frame-by-frame review finds no source identifiers
  or unrelated names; and
- the report states limitations, invalid pairs, deviations, and all attempted
  runs before making the bounded claim.

Archive a signed inventory of the manifest, raw-capture digest, source-to-alias
map digest, analyzer commit, generated table digests, test output digests,
review decision, and public artifact digests. The raw source IDs and alias map
remain private; the publication includes only the safe inventory fields,
aliases, aggregates, and reproducibility statement.
