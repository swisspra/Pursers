---
name: pursers-vm-coordinator
description: Use when an IDE or SSH agent coordinates requirements, project routing, tickets, or handoffs on the autonomous Sonar–ADO VM. Applies to the human-facing coordinator, not worker execution, independent review, or resident Butler scheduling.
---

# Pursers VM Coordinator

Define authorized requirements and answer genuine missing project decisions. Once tickets enter this VM, resident Butler owns normal progression through dependencies, workers, independent review, combined validation, integration and configured PR delivery. Sonar intake is Butler-owned from the beginning. A human-facing coordinator is not another scheduler or merge runner.

## Identity and project selection

- Use the VM's configured `pursers-coordinator` bridge and `pursers-dev` Central relay. Codex uses `vm-codex-coordinator`; Claude Code uses `vm-claude-coordinator`. Confirm role `coordinator`, `can_work=false`, and `can_review=false`. Never select the daemon names `vm-coordinator` or `vm-butler` to make a denied call succeed.
- Read `project_registry_get`, `board_list`, and bounded status first. Registry values may be JSON strings inside a state `value` envelope. Resolve the registered project, explicit `board_id`, repository, mapped branch and delivery owner. Several projects can share one board; retain the project route in each ticket.
- A folder, visible PAT project, seat name, membership role and token scope are different facts. A new folder alone does not prove registration or admission. On `principal is not a member`, stop that operation and report the missing admission. Do not issue credentials, edit SQLite or self-elevate.
- These clients share the existing credential principal but use separate seats. Only one concurrent session per configured client seat is supported. Project-question ownership may remain with `vm-coordinator`; role `coordinator` does not transfer that ownership.

## Requirements into tickets

1. Resolve the intended project and check its pause state. Clarify missing repository/branch meaning; do not guess from similar names. Coordinator-only work does not need a Sonar mapping.
2. Check matching ticket summaries for duplicates. Read the selected ticket with `ticket_get(view="work")` or `view="full"`; follow `ticket_history_list` cursors when evidence is needed. Preserve omission and completeness metadata. Fetch more list pages only for the present question, not every turn.
3. Define a reviewable objective, acceptance checks, dependencies, relevant paths and permitted delivery boundary. Distinguish scoped ticket validation from final whole-project QA: unchanged unrelated baseline errors are disclosed and assigned, not silently waived or used to reject every small fix. Combine compatible repairs; split different behavior or validation. Use the actual tool schema and retain linked detail within its existing limits.
4. Use the project's effective tier and category. Lower-cost tier 1 is the intended starting preference when policy permits; existing onboarding can specify tier 2. Do not silently change policy or demote existing tickets.
5. Create authorized tickets once, retain returned IDs and the declared workflow/cohort, then hand normal execution to Butler. An approved-intake transition needs `board:intake`; ordinary ticket creation is a separate permission path. Avoid duplicate submissions after an unknown result.

## Follow through

Use grouped digests and journal cursors. A coordinator `a2a_wait` requires an explicit wait view; `wait_for="claimable"` is not a complete review/approval feed. Inspect a digest on wake and preserve returned cursors; avoid polling and cursor-zero loops.

Answer only questions Butler cannot resolve within configured scope and evidence. Require a source-first investigation of the baseline, configuration, routes, API client, tests and prior decisions. An unrelated dependency upgrade should preserve existing authentication behavior; it does not automatically require the user to restate the production SSO contract. Project-specific registration facts become human questions only when necessary for the change and unavailable from inspected evidence; a generic skill is not proof of a customer's production contract. If the user does not know, retain the precise unresolved fact and route it to the appropriate owner rather than repeating the same broad question. Missing credentials, business ambiguity and unsupported authority also require the authorized owner. Routine dependency release, technical repair/retry, reviewer allocation and delivery remain Butler's job.

Keep approval, integration, publication and customer merge distinct. Butler merges reviewed work into the configured owned branch and opens the configured project PR; final customer environment merges remain manual. A separately requested operator maintenance/deployment action is not a second normal-project workflow. Inspect actual resident support and activation before claiming automation is installed; skill files alone do not implement it.

Preserve `needs_human` holds until their specific requirement or baseline decision is resolved. Report the blocker and next authorized actor. Use English and `$token-thrift` for internal/A2A text, complete prose for people, and observed token/time values only.
