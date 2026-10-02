# Reviewer scenarios

Verify these cases when they apply; report what was exercised.

| Scenario | Required outcome |
| --- | --- |
| Credentials absent | Fail without printing a secret or suggesting a pasted token. |
| Wrong board or read-only identity | Refuse the review mutation and identify the missing membership or capability. |
| Existing skill collision | Installer preview reports the conflict; no file changes without explicit replacement. |
| Expired or resumed lease | Refetch authoritative state and never continue an unowned review. |
| Blocked baseline | Reproduce the selected suite and compare the approved base before assigning causality. |
| Review conflict | Stop approval, preserve both evidence sets, and request the authorized resolution path. |
| Unauthorized release | Reject merge, tag, publish, deploy, restart, or credential changes outside explicit authority. |

Also verify that a worker principal cannot review its own submission, the branch
and full SHA exist, the exact tip diff matches the declared files, and generated
outputs are left to their named owner.
