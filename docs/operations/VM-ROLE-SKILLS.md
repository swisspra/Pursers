# Private VM role skills

The Sonar–ADO VM has two role-specific skills in
[`integrations/vm-skills`](../../integrations/vm-skills). They supplement the
four portable Pursers skills; they do not change that public bundle, product
versions, credentials, registry ownership or runtime authorization.

| Skill | Intended reader | Responsibilities |
| --- | --- | --- |
| [`pursers-vm-coordinator`](../../integrations/vm-skills/pursers-vm-coordinator/SKILL.md) | Human-facing IDE/SSH coordinator | Resolve projects, turn authorized requirements into reviewable tickets, follow progress and hand off to the configured delivery owner |
| [`pursers-vm-butler`](../../integrations/vm-skills/pursers-vm-butler/SKILL.md) | An agent supervising or diagnosing resident Butler | Understand Sonar intake/grouping, shared fleet capacity, executor controls, integration and publication |

The coordinator skill does not tell the coordinator to claim or review work.
The Butler skill does not tell an interactive session to take the service's
identity. Both preserve the Sonar and human-requirement routes and separate
ticket approval, integration, publication and customer merge.

The VM operating contract assigns both routes to Butler after ticket creation:
Coordinator supplies requirements and genuinely missing answers; Butler handles
dependency release, agents, independent review, combined validation and the
configured owned-branch integration/PR. Butler investigates existing source,
configuration and tests before asking a domain question. Preserve existing SSO2
behavior during unrelated upgrades; escalate an unavailable registration fact
only when implementation actually depends on it. Per-ticket validation compares
the requested scope with its exact baseline; final QA retains whole-project
checks. These instructions differ from the portable default workflow.

Native progression support is tracked in the
[implementation plan](../superpowers/plans/2026-10-25-butler-owned-workflows.md).
Installing the revised skills does not mean that candidate code has passed
review, been deployed or been activated.

## Repository access for both routes

A coordinator can supply a requirement without exposing its upstream PAT to
the model. The registered repository still needs an approved credential
reference for the actors that fetch, push or integrate its source. Central
membership and scopes do not supply Azure repository access.

Worker event runners inject the configured Git credential helper into their
child environment. The resident Butler does not automatically inherit that
environment. Its verified fleet clone must resolve the same approved
repository-specific credential reference through the supported helper. Keep
that reference and the existing secret outside Git; do not embed a PAT in a
remote URL or copy worker credentials into a prompt. Preserve unrelated clones
and global Git configuration when configuring the registered clone.

The approved Unified MCP connector supplies the configured Azure tools through
its own protected authentication binding. Before workflow activation, verify a
read-only Git ref and the allowed repository-details tool against the exact
mapped repository and branch. Record those observations separately from
successful integration or PR publication, which require their own evidence.

## Verified submissions from isolated worktrees

Implementation and tests stay in the ticket's isolated worktree. The relay's
`ticket_submit(repository=...)` argument serves a different purpose: it selects
an allowed clone from which to verify the exact origin branch tip and sign the
submission proof using the caller's own credential.

If an authorized worktree is outside `--repository-root`, use its owning
registered clone under the existing permitted root for this argument. Verify
that both paths share the intended repository and origin first. The current
`verify_remote_submission` checks the branch and full SHA in
`branch_and_commit` with `git ls-remote`; it does not use the common clone's
checked-out HEAD. Do not switch or modify that shared checkout, copy changes
there, borrow a credential, or broaden the relay to the home directory.

Record the tested worktree source separately from the signing clone. The
independent reviewer still verifies the exact pushed cumulative source and
results; a passing remote-tip preflight is not a code review.

## Worker checkpoint communication

Ordinary workers do not hold `board:coordinate`; `ticket_annotate` is an
administrative operation. Do not ask a worker to elevate its role or stop solely
because it cannot add an annotation. A worker can send a durable
`ticket_question_ask` with `kind="information"`, its own ticket ID, the exact
pushed branch/SHA and actual test results. This channel does not pause its lease.
The registered coordinator verifies the checkpoint and relays an attributed
annotation to the other owner while independent work continues.

Use `ticket_request_human` for a real missing decision or exhausted authorized
recovery, rather than a routine progress message. A dependency wait should name
the exact missing committed input and preserve the current checkpoint.

The same rule applies to permission to continue. Fixing owned type/lint/test
defects, committing, pushing a ticket branch and submitting the authorized scope
are ordinary work. Butler handles technical continuation and trusted workspace
lookup. Reviewers compare the exact accepted baseline and candidate: a fixed
target with passing affected checks and unchanged unrelated baseline failures is
eligible for scoped approval, with the remaining failures disclosed. Owned
defects still fail review; final combined QA retains its full configured checks.

Apply instruction updates to the actual event-runner prompts and existing
Goose `AGENTS.md`/`.goosehints`, as well as the generator and discoverable skills.
The daemon and Goose fleet do not automatically inherit an IDE skill edit.
Retain a hash receipt and rollback copy, preserve identity/configuration and
active leases, and let an active event finish rather than restarting it just to
reload text. New event invocations read the updated instructions.

Autonomous mode also requires backlog supervision: event wakes plus a bounded
periodic status sweep, fair per-project deadlines, and separate observation,
progress and service-liveness timestamps. A running process is not proof that
pending tickets are supervised. Status reads stay deterministic; unchanged
inputs reuse model decisions. Recovery authority remains explicit and scoped to
the activated project, with real domain/security decisions held for their owner.
These instructions specify the operating contract; installing them alone does
not add missing runtime scheduling or recovery tool execution.

For stacked development candidates, report all cumulative `files_changed`
truthfully while separating inherited prerequisite paths from the owned delta.
A scoped component approval does not approve unfinished inherited workflow code;
the final integrated capability still requires its exact cumulative review.

## Discover and invoke on the VM

Installed copies are exposed to Codex through
`$HOME/.agents/skills/pursers-vm-coordinator` and
`$HOME/.agents/skills/pursers-vm-butler`. Claude Code sees the corresponding
entries under `$HOME/.claude/skills`. Each is a symlink to an owner-only,
content-addressed directory under `$HOME/services/pursers/skill-releases`.
Per-release receipts contain file hashes and the created host links. Existing
portable skills and MCP/model/provider configuration are unchanged.

Open a new remote session or reload its skill list. In Codex:

```text
Use $pursers-vm-coordinator to inspect the registry and draft tickets for my requirement.
```

For Butler operations:

```text
Use $pursers-vm-butler to explain the current source intake and fleet decisions from live evidence.
```

Claude Code supports `/pursers-vm-coordinator` and `/pursers-vm-butler`.
The installed skills are available for normal relevance-based selection;
explicit invocation chooses the intended role unambiguously. See the official
[Claude Code skills guide](https://code.claude.com/docs/en/skills) and
[Codex skills guide](https://learn.chatgpt.com/docs/build-skills).

## What the resident actually loads

The resident Butler is a Python service. Its constrained model requests are
constructed in `tools/board-butler/board_butler.py` and
`tools/board-butler/source_grouping.py`; it has no Agent Skills discovery loop.
The direct backend returns a proposal and citations, while code validates and
executes permitted connector or executor operations.

Installing `pursers-vm-butler` therefore equips an IDE agent to operate and
diagnose the service. It does not inject the entire skill into every paid
request, activate a saved delivery policy, start seats, or replace the daemon's
prompts. A future request to make the resident consume additional instructions
needs a separately designed prompt boundary and code validation. Keeping the
operating manual outside routine provider requests avoids paying for it on
every empty-queue check.

## Dated observations and drift

The Butler reference records the verified 2026-10-25 behavior: a 20-seat host
envelope, Goose event templates, no ACP worker allocation, source-specific
in-flight/canary limits, different control-board and Sonar-onboarding tiers,
legacy grouping chunk limits, and per-project delivery differences. These are
diagnostic context, not configuration values to apply to every new project.
The skills require reading effective configuration before acting.

The skills also preserve two admission boundaries: a seat role is distinct from
principal membership/token scope, and a project coordinator's question ownership
does not automatically transfer to an interactive coordinator. Loading a skill
does not bypass either check.

## Validate and update

Validate frontmatter and metadata with the skill creator's `quick_validate.py`,
then check reference links, file hashes, host discovery and relevant live
contracts. The 2026-10-25 install exercised Codex app-server `skills/list`
without creating a thread or making a model request. Both skills were enabled
with no discovery errors. Claude's files and links were verified; no paid
Claude session was used to test behavioral adherence.

Keep immutable installed releases and compare the exact file hashes before
updating links. Preserve a conflicting skill directory for review instead of
merging files into it. Record the source commit and installation receipt.
Rollback removes only these two host links or restores the previous recorded
targets; it does not remove shared skill roots or change Central state.

The deployment's existing [VM guide](SONAR-ADO-VM.md) describes the intake and
delivery boundary. [Projection recovery](PROJECTION-RECOVERY-5.2.1.md) explains
summary-first reads, full retained detail and the daemon's bounded model inputs.
