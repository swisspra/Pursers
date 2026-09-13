# Home acceptance harness

The harness independently reruns every suite named in an acceptance report.
For `candidate-diff-check`, it checks only the authored candidate commit against
that commit's own parent.

On a GitHub `pull_request` run, `GITHUB_SHA` names the synthetic merge checkout,
not the authored pull-request head. The harness therefore reads
`pull_request.head.sha` from `GITHUB_EVENT_PATH`, verifies that exact commit is
available locally, and runs `git diff --check <head>^ <head>`. If the event
payload is unavailable, a two-parent merge checkout falls back to the verified
`git rev-parse HEAD^2` commit. CI checks out three history levels so the
synthetic merge, authored head, and authored head's parent are all available to
the candidate-only diff.

Push runs keep their original behavior: the report candidate must equal the
verified `GITHUB_SHA`, and the harness checks that commit against its parent.
Local runs without GitHub metadata use the verified report candidate directly.
This prevents unrelated changes between a stale PR base and the candidate's
real parent from contaminating the candidate-only whitespace gate.
