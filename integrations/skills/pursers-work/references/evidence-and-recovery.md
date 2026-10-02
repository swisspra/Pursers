# Evidence and recovery

## Exact submission evidence

- `branch_and_commit`: `<exact-branch>@<verified-full-SHA>`.
- `files_changed`: paths from the exact tip commit, not the cumulative branch.
- `test_output`: literal commands and results, including relevant failures.
- `observations`: compatibility, rollback, generated-artifact drift, and limits.

Use the ticket's required field spelling. If a current server rejects a newer
structured field, preserve the same evidence in notes and report the version
mismatch; do not invent success.

## Lease expired or session resumed

Refetch the ticket. If it is open, wait for or accept only a new valid offer. If
another worker owns it, stop. When continuation names a previous branch and
commit, fetch and verify that exact source before changing it. Never renew a
lease merely to keep an idle session alive.

## Blocked baseline

Run the selected affected test before claiming that the environment is blocked.
Record the command, exit code, and failure. Determine whether the same failure
exists on the approved base. Fix only failures within ticket scope; request human
input when the required decision or authority is missing.

## Rejection round

Return only after a new valid offer and claim. Read the latest verdict and all
new coordinator annotations before retrying a failed gate. Keep the same durable
ticket, verify the new tip, and resubmit corrected exact evidence.

## Generated outputs

When a lock, checksum manifest, or generated reference is operator-owned, list
the drift and its supported refresh command or owner. Do not edit the output by
hand or disguise it as source work.
