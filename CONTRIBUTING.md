# Contributing to Pursers

Thanks for helping improve Pursers. Please keep changes focused, testable, and
safe for a local coordination service that handles credentials and durable
work records.

## Development setup

Use Python 3.12 for parity with CI. From a fresh clone:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python \
  --editable packages/client \
  --editable packages/central \
  --editable 'packages/personal[test]' \
  --editable packages/import \
  --editable tools/wait-bridge
uv pip install --python .venv/bin/python --no-deps --editable packages/pursers
```

The test manifest adds checkout package sources to `PYTHONPATH`, so tests use
the code in this clone rather than an operator-installed package. Run the same
suite inventory as CI:

```sh
.venv/bin/python tools/ci_manifest.py check
.venv/bin/python tools/ci_manifest.py collect --output /tmp/pursers-counts.json
.venv/bin/python tools/ci_manifest.py run
.venv/bin/python tools/ci_manifest.py verify \
  --input /tmp/pursers-counts.json
.venv/bin/python tools/leak_scan.py
git diff --check
```

For dashboard changes, also run `npm ci` and `npm run typecheck` in
`tools/dashboard-ui`. Run narrower tests while iterating, but run every affected
suite before submitting.

## Ticket and review workflow

1. Work only on a ticket you are authorized to claim.
2. Start from the ticket's approved base in an isolated branch or worktree.
3. Keep the change inside the ticket's bounded scope.
4. Preserve unrelated local changes and never commit credentials or private paths.
5. Renew the ticket lease during long work.
6. Run the affected suites, leak scan, and `git diff --check`.
7. Commit and push the exact branch that was tested.
8. Submit the full commit SHA, exact changed-file list, and literal test evidence.
9. A reviewer under a different principal independently checks that exact SHA.
10. Address a rejection only after the ticket is reoffered and claimed again.
11. Reuse the ticket branch for rejection fixes when the fix allows it; do not
    create one remote branch per attempt.
12. After a ticket closes, delete your own remote ticket branch. Never delete a
    branch that backs a live ticket or a submission awaiting review.

## Commits and pull requests

Use a short imperative subject, optionally with a useful component scope, such
as `fix(wait-bridge): preserve the authoritative cursor`. Keep mechanical or
generated changes separate from behavioral changes when that makes review
clearer. Explain why the change is needed, not just what the diff says.

Pull requests should name the ticket, summarize risk, list the exact commands
run, and call out documentation or compatibility changes. Do not edit release
versions outside an authorized release-train ticket. Developer Certificate of
Origin sign-off (`Signed-off-by`) is not required.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Report security issues privately as described in [SECURITY.md](SECURITY.md).
