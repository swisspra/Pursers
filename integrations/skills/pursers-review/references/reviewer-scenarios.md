# Reviewer scenarios

Verify these cases when they apply; report what was exercised.

| Scenario | Required outcome |
| --- | --- |
| Credentials absent | Fail without printing a secret or suggesting a pasted token. |
| Wrong board or read-only identity | Refuse the review mutation and identify the missing membership or capability. |
| Existing skill collision | Installer preview reports the conflict; no file changes without explicit replacement. |
| Expired or resumed lease | Refetch authoritative state and never continue an unowned review. |
| Blocked baseline | Reproduce the selected suite and compare the approved base before assigning causality. |
| Target fixed; unrelated baseline findings unchanged | Approve the exact scoped candidate when affected checks and required evidence pass; disclose remaining findings and their owners. Do not require unrelated whole-app cleanup. |
| Candidate introduces owned compiler/lint/test errors | Reject with concrete repair instructions within the existing ticket; a baseline elsewhere does not excuse these errors. |
| Worker asks whether to submit an eligible scoped candidate | Submission and independent review are the normal next steps; do not require another permission cycle merely because disclosed unrelated baseline failures remain. |
| Final combined-QA candidate | Apply all configured final checks to the exact combined source; scoped member approvals do not waive this gate. |
| Review conflict | Stop approval, preserve both evidence sets, and request the authorized resolution path. |
| Unauthorized release | Reject merge, tag, publish, deploy, restart, or credential changes outside explicit authority. |

Also verify that a worker principal cannot review its own submission, the branch
and full SHA exist, the exact tip diff matches the declared files, and generated
outputs are left to their named owner.
