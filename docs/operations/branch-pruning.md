# Remote branch audit and pruning

Audit first. The audit refreshes local remote-tracking refs, compares them with
all remote heads, and reads ticket status from every selected Pursers board. It
does not modify the board or remote branches.

```sh
python3 tools/branch_audit.py \
  --central-url https://CENTRAL.EXAMPLE/mcp \
  --token-file /PATH/TO/SEAT.jwt \
  --ca-file /PATH/TO/ca.pem \
  --board pursers \
  --json-out /tmp/pursers-branch-audit.json
```

The human table prints all four bucket counts and the ten oldest candidates in
each deletion tier. The JSON contains every branch, its exact audited tip and
last commit date, the ticket and board-derived status, protection reasons, and
candidate tier:

- Tier A: merged into `main` and not protected.
- Tier B: unmerged with a terminal board ticket and not protected.

Inspect the plan without deleting anything. This command must exit 2 and say it
is refusing to delete because the confirmation flag is absent:

```sh
python3 tools/prune_branches.py /tmp/pursers-branch-audit.json --tier A
```

Only the operator may execute a reviewed plan:

```sh
python3 tools/prune_branches.py /tmp/pursers-branch-audit.json \
  --tier A --tier B --confirm-delete
```

Immediately before deletion, the script verifies that every remote tip still
matches the audited SHA. It always rejects `main`, the configured default
branch, `integration/*`, live-ticket branches, and branches or commits backing
a submission awaiting review. Re-run the audit if any remote tip moved.
