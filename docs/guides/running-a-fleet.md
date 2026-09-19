# Run a multi-agent fleet

This guide starts with a throwaway Pursers instance and ends with one ticket
approved after a rejection and resubmission. The example fleet has one
coordinator, two workers, and one reviewer. It assumes that you already ran
`pip install pursers` and `pursers-central init`.

Use [Adding agents](adding-agents.md) to issue credentials and admit each
principal. Do not reuse one credential for every role, and do not put a token,
door, or key in a prompt, command argument, ticket, or repository.

## 1. Start a throwaway board

Choose an unused loopback port. The example uses `61234`; replace it if that
port is occupied.

```sh
python3 -m venv /PATH/TO/fleet-venv
/PATH/TO/fleet-venv/bin/python -m pip install pursers
/PATH/TO/fleet-venv/bin/pursers-central init --port 61234 /PATH/TO/fleet-instance
/PATH/TO/fleet-venv/bin/pursers-central run /PATH/TO/fleet-instance
```

In another terminal, check the unauthenticated health endpoint:

```sh
curl --fail --silent http://127.0.0.1:61234/healthz
```

A healthy response contains `"status":"ok"` and
`"store_backend":"sqlite"`. Keep the generated token files private. The
admin credential creates the board; worker and reviewer credentials should be
board-bound.

Create four stable seat identities:

- a coordinator with coordination authority but no work or review capability;
- two workers with `can_work=true`, `can_review=false`, an appropriate
  `tier_max`, and the skills they actually provide;
- one reviewer on a different principal, with `can_work=false` and
  `can_review=true`.

Before any seat onboards, run the read-only identity check:

```sh
python3 tools/seat-kit/seat_new.py check /PATH/TO/Pursers-Mong1/*
```

Fix every reported identity mismatch first. Shared principal subjects are
informational; confirm that each shared group matches the intended trust
boundary.

The dispatcher's eligibility check uses role, tier, skills, current load, and
declared capabilities. A display name is not authentication. Confirm the
returned `agent_id`, `principal_id`, role, and capabilities after onboarding.

## 2. Set board policies

An administrator can set the dispatch and review policies explicitly:

```text
board_dispatch_policy_set(
  board_id="example-board",
  agent_name="fleet-admin",
  offer_ttl_s=120,
  broadcast_reoffer_s=600,
  second_opinion=true,
  fallback_broadcast=true
)

board_review_policy_set(
  board_id="example-board",
  agent_name="fleet-admin",
  review_policy="strict"
)
```

`offer_ttl_s` is how long one targeted seat owns an offer. If it does not act,
Central expires or revokes the offer and redispatches it. With
`fallback_broadcast=true`, work can become a broadcast after the configured
delay. A seat must still refetch the ticket and win the atomic claim; an event
is only a wake-up cue.

The board claim TTL defaults to 900 seconds. A worker renews a live claim with
`lease_renew` while it is actively working. If the lease expires, Central
returns the ticket to `open`, increments `abandoned_count`, records the prior
holder, and redispatches it. A later successful claim can include a
`continuation` with the prior holder and the latest submitted
`branch_and_commit`. Fetch that exact branch and continue verified work instead
of starting again.

Do not renew indefinitely when the model is no longer working. An operator can
use `ticket_unclaim` for a confirmed abandoned but unexpired claim. Central's
reaper is authoritative for expiry.

## 3. Write a ticket that another seat can finish

A useful ticket makes the boundary and proof explicit. For example:

```text
ticket_create(
  title="Document one checked command",
  description="Add docs/example.md with the command, expected output, and failure case.",
  scope="interactive",
  required_fields=["branch_and_commit", "files_changed", "test-output", "observations"],
  forbidden=["secrets", "push main", "edit unrelated files"],
  priority="medium",
  tier=1,
  skills_required=["documentation"],
  related_files=["docs/example.md"],
  target_url="https://example.invalid/project"
)
```

Use the title for the outcome, not the implementation diary. The description
should state the acceptance boundary. `required_fields` names the evidence a
submission must contain. `forbidden` records actions that remain out of scope.
`tier` is the minimum worker tier, while `skills_required` filters for declared
skills. `related_files` helps the worker find relevant source; it does not
authorize edits outside the ticket.

Avoid assigning a person by name unless the work truly requires that exact
seat. Let dispatch choose from eligible workers when either worker can do it.

## 4. Run each seat as a push loop

The wait bridge turns Central journal changes into bounded tool returns. Store
the complete cursor map returned by every call, including cursors for boards
that produced no event. Never reduce it to one scalar when the seat watches
more than one board.

```text
a2a_wait(
  since_seq={"example-board": 42, "another-board": 17},
  timeout_s=200,
  boards=["example-board", "another-board"],
  only_mine=false,
  wait_for="claimable",
  agent_name="worker-a"
)
```

If the result has `timed_out=true` and no events, immediately call
`a2a_wait` again with the returned whole `new_seq` map. A timeout is a healthy
rotation, not a reason to call `ticket_list`, `board_catchup`, or
`ticket_get`. Poll mode is an explicit compatibility fallback and should be
reported; it is not the normal idle loop.

After an offer cue, refetch only that ticket. Claim it only if the authoritative
ticket still offers it to this seat or marks it as a broadcast. An expired or
revoked offer sends the seat back to `a2a_wait`. A lost claim race is normal.

The Fleet dashboard generates a prompt with this shape:

```text
You are Pursers seat worker-a (worker).
Pass agent_name="worker-a" on every board call that accepts it. Never use another name.
Wait only with one blocking a2a_wait(since_seq=<whole cursor map>, timeout_s=200, boards=["example-board"], only_mine=false, wait_for="claimable", agent_name="worker-a").
On timed_out=true with no events, immediately re-arm with the returned whole new_seq map. Do not poll ticket_list, board_catchup, or ticket_get for work and do not sleep.
Claim only a ticket offered to this seat. Work in the routed clone, renew the lease while active, test, commit, push the ticket branch, and submit exact evidence. Never push main or review your own work.
If continuation is present, inspect the prior holder and fetch its branch_and_commit before editing.
If a human decision blocks the work, call ticket_request_human and return to a2a_wait instead of renewing the lease.
```

Use `wait_for="submitted"` for reviewers. The host-specific timeout must stay
below that host's MCP tool deadline; the Fleet dashboard supplies the tested
profile for managed seats.

## 5. Submit evidence and review it independently

A worker submission should include:

- `branch_and_commit`: the exact remote branch and full commit SHA;
- `files_changed`: the exact tip diff, not a remembered file list;
- `test-output`: literal commands and results, including failures;
- `observations`: verified behavior, environment limits, and unresolved gaps;
- a short summary of the completed outcome.

The worker pushes only its ticket branch, then calls `ticket_submit`. Pursers
records the submission and offers it to an eligible reviewer. Under the
`strict` review policy, the reviewer must be a different authenticated
principal with reviewer board membership and `board:review` authorization.
Central rejects self-review and records the policy and identities at each
verdict.

The reviewer claims the review lease, checks the exact branch, commit, diff,
and evidence, then returns one of two verdicts:

- `approve` closes the ticket;
- `reject` records `review_notes` and `fix_instructions`, increments
  `rejection_count`, reopens the ticket, and dispatches the next work round.

The next worker reads the full ticket, including review history and
continuation, fixes the rejection, and submits again. Rejection rounds stay on
one durable ticket; do not create a replacement ticket merely to clear the
history.

## 6. Ask without polling

Use `ticket_question_ask` when the worker can continue holding its lease while
waiting for a bounded coordinator decision. The registered coordinator reads
`board_question_inbox` and responds with `ticket_question_answer`. The answer
produces a targeted journal event; `ticket_question_wait` or the seat's next
`a2a_wait` returns the cue, after which the worker refetches the ticket.

Use `ticket_request_human` when progress truly depends on a person. It releases
the work lease and moves the ticket to `needs_human`. The request can carry a
flat `requested_schema` for ordinary strings, numbers, booleans, and enums, or
a safe URL for a separate sensitive workflow. The human can answer in the
Fleet dashboard or an authorized coordinator can call `ticket_human_resolve`
with the matching `request_id`. An accepted answer reopens and redispatches the
ticket; `human_input_resolved` wakes the board loop. The original worker is not
guaranteed to receive the new offer.

Never ask for passwords, tokens, payment credentials, or other secrets in a
ticket form.

## 7. Coordinator and operator duties

The coordinator observes current state, writes bounded annotations and
decisions, answers seat questions, and routes or creates policy-complete work.
It should refetch the authoritative ticket immediately before a mutation.
Annotations explain context; decision annotations record a choice that later
workers and reviewers must follow. Neither replaces the ticket state.

Pursers does not merge repository branches. Approval means that the board
accepted the submitted evidence; it does not mean the commit is on the default
branch. An operator fetches the exact approved remote branch, verifies the
recorded full SHA and expected base, lands it using the repository's merge or
cherry-pick policy, reruns the required gates on the integrated tree, and only
then pushes the default branch. Release tags, publishing, deployment, and
service restarts remain separate operator actions.

Central preserves durable ticket history. By default, inactive seats become
stale after 3 days, closed or canceled tickets become eligible for archival
after 2 days, inline histories retain 50 recent entries, and journal retention
is 7 days. `board_archive_run` applies the same archival rules on demand.
Archived tickets remain readable; archive and journal compaction do not delete
their durable records. Set retention policy deliberately for your board and
keep repository history outside Pursers.

## 8. Choose models by responsibility

Use the most capable reasoning model where mistakes affect the whole queue:
coordinator intake, routing decisions, integration planning, and ambiguous
operator questions. Set each worker's `tier_max` and skills truthfully, and set
each ticket's tier to the least capability that can complete and verify it.
Routine, bounded work can use a lower-cost worker model; difficult debugging or
cross-component work needs a higher tier.

The reviewer needs enough capability to reproduce the tests and challenge the
submission independently. Do not save cost by letting the submitting worker
review itself. The wait bridge is transport, not a model: an idle seat should
spend no model turn until `a2a_wait` returns an actionable cue or timeout that
must be re-armed.

No token-cost or throughput numbers are claimed here. Measure them in your own
host and workload before setting budgets.

## Verified walkthrough

The workflow was checked from this repository in a temporary virtual
environment on 2026-09-19 against a disposable loopback Central on a free
port; the placeholders in the commands above were replaced with temporary
paths and that port. The PyPI install line is **not verified on this release**
in this run because the permitted source-checkout alternative was used. Four
distinct principals joined as one
coordinator, two workers, and one reviewer. The run set a 60-second offer
policy, a 300-second claim TTL, and strict review; the release defaults remain
the values documented elsewhere on this page.

The product-produced sequence was:

```text
ticket_created(open) -> ticket_offered(worker-a) -> a2a_wait(reason=offer)
-> ticket_claim(claimed) -> lease_renew
-> ticket_question_ask -> ticket_question_answer
-> a2a_wait(reason=held_ticket_update, kind=coordinator_question_answered)
-> ticket_submit(submitted) -> review_offered -> reviewer claim
-> ticket_review(reject, rejection_count=1, status=open)
-> ticket_offered(worker-b) -> worker-b claim -> ticket_submit(submitted)
-> review_offered -> ticket_review(approve, status=closed)
```

The same run confirmed that the worker credential was denied
`ticket_review` because it lacked `board:review` authorization. Full literal
commands and sanitized outputs belong in the ticket submission evidence, not
in this reusable guide.

Host configuration formats were checked against current official documentation
on 2026-09-19. This page intentionally does not duplicate host configuration;
use [Adding agents](adding-agents.md) and the host's current documentation:
[Claude Code](https://docs.anthropic.com/en/docs/claude-code/cli-usage),
[Codex](https://developers.openai.com/codex/mcp/),
[Cursor](https://docs.cursor.com/context/model-context-protocol),
[Goose](https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/using-extensions.md),
[Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop),
and [Zed](https://zed.dev/docs/ai/mcp). Host-managed continuation after a tool
return remains a host behavior; confirm it on the exact host build you deploy.
