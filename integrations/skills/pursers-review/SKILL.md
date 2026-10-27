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
5. Re-run the relevant tests and repository checks on the exact candidate.
   Compare failures with the immutable accepted baseline using the same commands
   and environment. Do not approve based only on pasted output or a green
   unrelated suite.
6. Approve only when the exact source satisfies the ticket. Otherwise reject
   with concrete findings and fix instructions tied to files or observable
   behavior. A review conflict or unavailable source is not approval.
7. Release the review lease after the verdict. Never merge, publish, deploy, or
   widen authority unless a separate explicit operation authorizes it.

## Scoped implementation review and final QA

Judge an implementation ticket against its declared target and authorized scope.
When the target is fixed, affected tests pass and no regression is introduced,
unchanged failures outside that scope do not justify rejection or a human hold.
For example, fixing one of ten baseline findings can satisfy that ticket while
the remaining nine are recorded for their owners. Establish this with actual
base/candidate evidence; unchanged source paths alone do not prove unchanged
behavior after a dependency upgrade.

Approve eligible scoped work with the exact SHA, affected-test results, baseline
comparison and remaining findings disclosed. State that this is scoped acceptance,
not whole-project success. Reject an unfixed target, introduced regression,
owned compiler/lint/format defect, missing required evidence or an unauthorized
change. Ask for a concrete repair within the existing scope, not new permission
to perform work the ticket already authorizes.

The final combined-QA ticket has a different scope: enforce its full configured
checks on the exact combined revision. Neither an implementation approval nor a
baseline disclosure waives that gate, a security decision or independent review.

Read [reviewer scenarios](references/reviewer-scenarios.md) for required failure
paths. The canonical lifecycle is documented in the
[fleet guide](https://github.com/swisspra/Pursers/blob/main/docs/guides/running-a-fleet.md).
