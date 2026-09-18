# Stranded approval audit

An approved ticket is not proof that its content reached `main`. Run the
stranded-approval audit before release bookkeeping and after any batch of
operator merges:

```sh
TMPDIR=/PATH/TO/HOME/CACHE/stranded-approvals \
PYTHONPATH=packages/client/src \
python3 tools/stranded_approvals.py \
  --central-url https://127.0.0.1:8766/mcp \
  --token-file /PATH/TO/WORKER_TOKEN \
  --ca-file /PATH/TO/CA.pem \
  --board pursers \
  --agent-name WORKER_SEAT \
  --role worker
```

The command uses the normal board client and the named seat's existing role.
It explicitly requests closed and archived tickets, enumerates each ticket
status, and then reads full ticket records in 25-ID batches. It stops without
an audit result if those pages do not cover the board's reported ticket total.
The token is read only from the named file and is never printed.

For each ticket whose current `review_verdict` is `approve`, the tool parses the
last submission's exact `branch_and_commit` line. An approved commit already
under `origin/main` is `LANDED_ANCESTOR`. Otherwise, the tool diffs the commit
from its merge base with `origin/main`, collects every added line, and checks
whether that line occurs in the same path on `origin/main`.

`LANDED_CONTENT` requires at least 90% of those added lines to be present. This
threshold puts the known rebased carriers `535/540` (99.1%) and `266/273`
(97.4%) above the line, while the known stranded changes `23/206` (11.2%) and
`15/441` (3.4%) remain far below it. A non-ancestor change with no added lines
is conservatively `STRANDED 0/0`; deletion-only equivalence needs independent
inspection rather than a content-presence guess.

Every approval produces one tab-separated row containing ticket ID, approved
SHA, state, and title. `UNVERIFIABLE` means the latest submission lacks exactly
one valid full-SHA `branch_and_commit` line. `BRANCH_MISSING` means the approved
commit object is unavailable after fetching the remote. The command exits 1 if
any row is `STRANDED`, 2 when it cannot complete the audit, and 0 otherwise.

The audit is evidence only. It never merges, deletes, tags, publishes, or
pushes. An operator decides how to reconcile every stranded row.
