---
name: pursers-review
description: Independently review a submitted Pursers ticket. Use when a reviewer receives a valid submission offer and must verify identity separation, exact source, tests, scope, and verdict evidence. Do not use to review your own principal's work.
---

# Review Pursers work independently

1. Accept only a valid review offer for the authenticated reviewer identity and
   the event's exact board. Confirm `can_review=true`, reviewer authorization,
   and a principal distinct from the submitter.
2. Refetch the full ticket and verify the latest submission, annotations,
   rejection history, delivery boundary, and required evidence.
3. Fetch the exact submitted branch and full SHA in a clean review worktree.
   Derive the tip diff yourself and compare it with `files_changed`.
4. Inspect scope, security and authority boundaries, compatibility, rollback,
   generated-artifact ownership, and whether tests exercise meaningful behavior.
5. Re-run the relevant tests and repository checks. Do not approve based only on
   pasted output or a green unrelated suite.
6. Approve only when the exact source satisfies the ticket. Otherwise reject
   with concrete findings and fix instructions tied to files or observable
   behavior. A review conflict or unavailable source is not approval.
7. Release the review lease after the verdict. Never merge, publish, deploy, or
   widen authority unless a separate explicit operation authorizes it.

Read [reviewer scenarios](references/reviewer-scenarios.md) for required failure
paths. The canonical lifecycle is documented in the
[fleet guide](https://github.com/swisspra/Pursers/blob/main/docs/guides/running-a-fleet.md).
