# Quality and evidence

Pursers treats a passing test command as evidence only when it is tied to an
exact source revision. The first GitHub Beta candidate is
[`28f81308d1cf3d40c4ed38091cc02d9c7d0827aa`](https://github.com/swisspra/Pursers/commit/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa).

## Required test suites

[`tools/ci_manifest.py`](../tools/ci_manifest.py) is the source of truth. It
rejects duplicate, missing, stale, or zero-test suite entries before CI runs
them. The measurements below were taken at
`f47fce79ec3473bde4a3b98ac42a758ee742d05a` with Python 3.14.7. Runtimes are
the single local pytest-reported samples from the canonical run, not benchmarks.

| Suite | Directory | Collected | Result | Runtime |
| --- | --- | ---: | --- | ---: |
| Central | `packages/central/tests` | 182 | 182 passed, 81 subtests passed | 25.18 s |
| Client | `packages/client/tests` | 88 | 88 passed | 4.59 s |
| Import | `packages/import/tests` | 106 | 106 passed | 11.07 s |
| Personal | `packages/personal/tests` | 196 | 195 passed, 1 skipped | 1.46 s |
| Wait bridge | `tools/wait-bridge/tests` | 253 | 253 passed, 47 subtests passed | 22.65 s |
| Fleet Dashboard | `tools/fleet-dashboard/tests` | 264 | 264 passed | 29.82 s |
| Coordinator | `tools/coordinator/tests` | 233 | 233 passed | 1.35 s |
| Worker runtime | `tools/worker-runtime/tests` | 106 | 106 passed | 11.58 s |
| Seat kit | `tools/seat-kit/tests` | 87 | 87 passed | 69.84 s |
| AionUi extension | `tools/aionui-extension/tests` | 9 | 9 passed | 0.14 s |
| Release tools | `tools/tests` | 50 | 50 passed | 45.62 s |

That is 1,574 collected tests: 1,573 passed and 1 skipped. Collection and
execution used the checkout-first `PYTHONPATH` assembled by the manifest. The
canonical repository sequence is:

```sh
python3 tools/ci_manifest.py check
python3 tools/ci_manifest.py collect --output /PATH/TO/counts.json
python3 tools/ci_manifest.py verify --input /PATH/TO/counts.json
python3 tools/ci_manifest.py run
```

## CI and CodeQL

The checked-in [`ci.yml`](../.github/workflows/ci.yml) runs on pull requests and
pushes to `main` or `nightly`. Its Python 3.12 job installs the pinned build
toolchain, builds all six checkout wheels, scans for leaks, proves isolated
wheel imports, checks release-version drift, collects every manifest suite,
runs them, and verifies nonzero collection. A separate Node 22 job installs the
dashboard dependencies and runs `tsc --noEmit`. A failure in either job makes
the CI run fail.

CodeQL uses GitHub default setup, so there is no repository-owned CodeQL YAML to
quote. Its generated workflow analyzes Actions, JavaScript/TypeScript, and
Python. Candidate acceptance requires all three exact-SHA analyses to complete
without an analysis error, warning, result, or unresolved alert.

Exact-candidate evidence:

- [CI run 34764545839](https://github.com/swisspra/Pursers/actions/runs/34764545839)
  completed successfully at `28f81308d1cf3d40c4ed38091cc02d9c7d0827aa`.
  Both `python-tests` and `dashboard-ui-typecheck` passed.
- [CodeQL run 34755018053](https://github.com/swisspra/Pursers/actions/runs/34755018053)
  completed successfully at the same SHA. Its three successful jobs were
  `Analyze (actions)`, `Analyze (javascript-typescript)`, and `Analyze (python)`.
  The exact-ref analyses loaded 17, 87, and 43 rules respectively, with zero
  results, warnings, errors, or open alerts.

These links prove the recorded run identity and conclusion. The release gate
also queries CodeQL analyses and open alerts for the exact candidate ref and
SHA; a green historical run alone is not a waiver.

## Deterministic artifacts

The candidate
[`release_versions.toml`](https://github.com/swisspra/Pursers/blob/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa/tools/release_versions.toml)
pins `SOURCE_DATE_EPOCH=315532800` and this build toolchain:

```text
build==1.3.0
setuptools==80.9.0
wheel==0.45.1
packaging==25.0
pyproject-hooks==1.2.0
```

Wheel builds use `PYTHONHASHSEED=0`, the pinned `SOURCE_DATE_EPOCH`, a clean
Python 3.12 environment, and `--no-isolation`. The cohort must contain exactly
these six distributions: `pursers`, `pursers-central`, `pursers-client`,
`pursers-personal`, `pursers-personal-import`, and `pursers-wait-bridge`.

```sh
export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH=315532800
# Home wheelhouse only:
export UV_PYTHON=/PATH/TO/python3.12
unset PIP_FIND_LINKS UV_FIND_LINKS
```

[`verify_publish_wheels.py`](../tools/verify_publish_wheels.py) checks the wheel
generator and the component hashes embedded in the Personal wheel. The
candidate
[`release.yml`](https://github.com/swisspra/Pursers/blob/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa/.github/workflows/release.yml)
also requires the exact manifest-derived filenames and writes
`SHA256SUMS.txt`.

Any change under `packages/central` or `packages/client` must regenerate
`packages/personal/src/pursers_personal/resources/component-lock.json` in the
same branch by running `tools/regenerate_component_lock.py`. Never edit the
component lock by hand; review must rebuild it independently and require
byte-for-byte equality.

The offline Home runtime wheelhouse uses the same two reproducibility variables
and additionally sets `UV_PYTHON` to its verified Python 3.12 interpreter. It
removes inherited `PIP_FIND_LINKS` and `UV_FIND_LINKS`, resolves all binary
dependencies, preserves the exact locally built Client and wait-bridge wheels,
then verifies an offline install, imports, command help, hashes, and
`wheelhouse.json`. See the candidate
[`build_home_runtime_wheelhouse.py`](https://github.com/swisspra/Pursers/blob/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa/tools/build_home_runtime_wheelhouse.py).

Its approved-hash lock contains separate sections for each supported runtime
platform. Refresh both Python 3.12 sections deliberately from a macOS arm64 host;
the Linux command uses pip's binary-only cross-platform resolver and does not
execute downloaded wheels:

```sh
python3 tools/build_home_runtime_wheelhouse.py --python /PATH/TO/python3.12 \
  --refresh-lock --platform macosx-11.0-arm64
python3 tools/build_home_runtime_wheelhouse.py --python /PATH/TO/python3.12 \
  --refresh-lock --platform linux-x86_64
```

The builder selects only the exact section matching `sysconfig.get_platform()`
and passes its pinned versions and hashes to pip. Linux CI remains the verifier
for the `linux-x86_64` section.

The AionUi ZIP does not need a time-related environment variable. Its
[`build.py`](https://github.com/swisspra/Pursers/blob/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa/tools/aionui-extension/build.py)
uses an explicit member allowlist, fixed `1980-01-01` timestamps, mode
`0100644`, DEFLATE level 9, and an embedded exact `HEAD` candidate manifest.
The gate builds the ZIP twice in clean directories and requires equality of
the archive name, size, SHA-256, candidate SHA, and every member's name, sizes,
and SHA-256.

## Governance and candidate approval

Boards default to strict review. Under this policy,
[`central.py`](../packages/central/src/pursers_central/central.py) labels the
verdict `independent-principal-review` and rejects approval unless the reviewer
has the reviewer role and `board:review` authorization under a principal
different from the submitter. Review evidence records the exact branch, commit,
changed files, and executable test commands and outputs.

The beta candidate went through five review rounds under that rule. rc1 was
rejected because the Apache-2.0 licence change and packaging notices were
missing. rc2 was rejected because the Hono 4.13.7 dependency lockfile already
on `main` had not been merged; rc3 was rejected when that lockfile was still
absent from the frozen tip. rc4 was rejected because deterministic artifact
hashes came from the parent of the final checksum commit, not the exact tip.
For rc5, the same source commit was retained, every artifact was rebuilt at
that exact tip, and the candidate was approved.

Each round preserved the reviewer's literal commands and outputs: ancestry of
every input, licence checks, the complete CI manifest run, a byte-for-byte
214-entry integration-manifest replay, double builds of the extension ZIP and
wheelhouse, and six pinned-toolchain wheels matched by hash. The practical
lesson is simple: build release artifacts only after the final checksum commit
exists. The final accepted source is the SHA linked at the top of this page;
internal ticket identifiers are intentionally omitted.

## Release gate

A release may proceed only when all of these conditions hold:

1. Version consumers and the Personal component lock match the single release
   manifest; leak scan, all 11 Python suites, dashboard typecheck/build, Node
   extension tests, dependency audit, and diff checks pass.
2. Six wheels and the AionUi ZIP rebuild byte-for-byte in separate clean
   directories; isolated installed imports and lifecycle probes pass.
3. Candidate CI and all three CodeQL categories bind the exact candidate SHA,
   with no unresolved candidate alerts.
4. An independent reviewer binds browser and host evidence to the same ZIP
   receipt and candidate SHA. A supported MCP Apps host claim additionally
   requires a rendered View, hostile-App negative control, and text fallback.
   Unsupported host behavior is recorded as a gap, never converted into a pass.
5. `origin/main`, the approved candidate, and the signed tag target are the
   same commit. Any changed byte creates a new candidate and restarts the gates.
6. The tag workflow rebuilds the six-wheel cohort and checksum file; downloaded
   GitHub and PyPI artifacts must match the approved hashes. Published versions
   are immutable.

The executable reference is the candidate
[`next-train-ship-plan.md`](https://github.com/swisspra/Pursers/blob/28f81308d1cf3d40c4ed38091cc02d9c7d0827aa/docs/release/next-train-ship-plan.md).

### Browser201 acceptance result for this beta

Independent Browser201 verification at
`dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5` executed all 29 AionUi extension
browser observations. Visual assertions passed for 23 and failed for 6:

- `extension-join.state.bridge-missing` — state copy "Install it, then retry."
  was not observed in the rc6 capture.
- `extension-join.state.error` — state copy "Join failed" was not observed in
  the rc6 capture.
- `extension-join.state.initial` — state copy "Not checked" was not observed in
  the rc6 capture.
- `extension-join.state.joining` — state copy "Joining" was not observed in the
  rc6 capture.
- `extension-join.state.status-empty` — state copy "Project not connected" was
  not observed in the rc6 capture.
- `extension.bounded-errors` — state copy "That ticket is not present in the
  bounded board response. Refresh and retry." was not observed in the rc6
  capture.

These are capture boundaries, not confirmed product defects. Evidence reference:
`CQ-3eddc8f73c6e7671`.

Strict evaluation recorded 2 passes, 6 failures, and 21 blocked AionUi rows.
The 21 blocked rows had visually passing browser captures but lacked their
required typed-evidence records; a visual pass alone is not a strict pass.

The verified beta surface is therefore the 29-row AionUi visual capture, not
the complete 201-row Browser201 gate. Fleet's 93 rows were not captured because
the live dashboard exposed none of the stable board selectors required by the
harness. Personal's 79 rows were not captured because this build exposed no
Personal browser route on the AionUi origin. Those surfaces, and typed evidence
for the 21 affected AionUi rows, remain explicitly not yet verified.

The beta.2 work is tracked as **Fleet dashboard: expose stable board selectors
the acceptance harness requires ([data-board-id], board selection state)**,
**Personal: provide the browser route the acceptance catalogue expects
(Personal MCP App / dashboard page on the AionUi origin) or re-scope the
personal.* rows**, and **Typed-evidence pipeline: make visual passes count -
wire typed evidence for the 21 AionUi rows strict-blocked without it**. These
boundaries preserve release-gate condition 4: unsupported or incomplete host
evidence remains a gap rather than a pass.

## Known gaps

All items below are tracked for beta.2. Titles are reproduced without internal
ticket identifiers:

- **Acceptance harness: candidate-diff-check assumes the checked-out HEAD is the candidate commit (false in PR merge checkouts)** — the harness compares the candidate to its first parent; a synthetic PR merge checkout can make that the wrong diff.
- **Capture sandbox runtimes must be detached from the launching seat's process tree** — captured runtimes can otherwise end when the launching seat exits.
- **Wait bridge: offer events are push-only; a seat that misses the push has no catch-up (reconcile-on-wait missing)** — a missed delivery can hide still-actionable work until another event arrives.
- **Central: no runnable module entry point or console script for first-time users** — beta.1 uses an explicit call to the runtime `main()` function.
- **Fleet dashboard: server-mode requests fail against a live Central with an opaque ExceptionGroup** — the error boundary needs a stable, actionable failure.
- **AionUi WebUI pairing needs a human step per long-lived core (automation pre-authorized, not yet shipped)** — the repeatable automated pairing path is not in beta.1.

Known gaps are not release evidence. They stay visible so a passing gate is not
mistaken for a claim that every host or operational path is already stable.
