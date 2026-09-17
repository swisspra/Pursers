# Pursers 5.0.0b2 release-train plan

Status: planning only. This document does not authorize version writes, a
candidate branch, a tag, publication, deployment, or a push to `main`.

## Planning basis

The last published release is `v5.0.0b1` at
`dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5`. Candidate assembly ticket
`TK-86f2fcaebee3` froze the current `origin/main` tip as the Beta.2 base:

```text
origin/main
0c83cd8da4e8ac04ee2dd564559f57718335cdef
```

`B2_BASE_SHA` is therefore
`0c83cd8da4e8ac04ee2dd564559f57718335cdef`. It contains the rc6 lineage
reconciliation at `86c89b59b851f5e50897aafeb84d4dada8d17735` and the later
reviewed train merges, including the board butler. Exact-tip GitHub Actions
run `35211638633` passed the `ci` workflow. CodeQL run `35211637611` passed
Actions, JavaScript/TypeScript, and Python analysis at the same SHA.

The scope below comes from `git log v5.0.0b1..B2_BASE_SHA` and the net tree
comparison `git diff v5.0.0b1..B2_BASE_SHA`. The comparison changes only
Central, Client, Personal, and Wait Bridge under the versioned package paths;
Import remains unchanged. The base's 190-entry host integration manifest
replays green. Candidate assembly must still regenerate and independently
replay that cumulative manifest after the version changes, commit that
manifest freeze, and only then define `B2_CANDIDATE_SHA` and build artifacts.

## Scope since `v5.0.0b1`

These ticketed changes are present in the frozen base tree. Each line states
the user-visible or release-visible effect; test-only work is labelled as
such.

| Ticket | Effect at `B2_BASE_SHA` |
| --- | --- |
| `TK-df1c517ceaf9` | Dispatch now honors live assignment targets instead of routing work to an ineligible seat. |
| `TK-3590242ea7d3` | Adds a public architecture guide for the product, runtime boundaries, and release train. |
| `TK-2d4fabaa7904` | Bounds Central journal responses and suppresses repeated same-principal seat-name collision noise. |
| `TK-3b12af87cea8` | Adds the beta getting-started manual and aligns its AionCore guidance with the candidate. |
| `TK-3b17c3239aaa` | Replaces the reverted README attempt with the reviewed public-beta README and release-train marker. |
| `TK-e7336274079e` | Adds the public quality, evidence, CI, CodeQL, artifact, and review contract. |
| `TK-d852c587b5e8` | Adds a bounded comparison with alternative agent frameworks. |
| `TK-d739337bb362` | Adds the Beta.1 changelog and release-note record later used as the rollback reference. |
| `TK-98d3c50356e6` | Adds contributor, conduct, security, issue, PR, and dependency-maintenance guidance for a public repository. |
| `TK-725814666af9` | Records a UX audit of Fleet, Personal, and AionUi surfaces with follow-up priorities. |
| `TK-685c1f664b99` | Adds the static `pursers.app` landing page and its deployment definition. |
| `TK-6e4c1b2cc855` | Adds the beta demo script and recording guide. |
| `TK-5745c9609b83` | Adds the public roadmap, discussion guidance, announcement, and GitHub Discussion templates. |
| `TK-45b22a93b28a` | Adds seven verified product screenshots and their provenance guide. |
| `TK-f8612c8553c8` | Makes Central runnable with a documented module entry point and health route. |
| `TK-50a3c268c655` | Documents Beta.1 operational traps in getting-started and release notes. |
| `TK-d5770eaa9dd5` | AionUi now renders startup health and the complete seat set instead of masking partial startup state. |
| `TK-a4972ef8fab0` | Fleet reports actionable TLS mismatch diagnostics instead of a generic disconnected state. |
| `TK-22b50041810d` | Fleet reuses one joined board client and event loop per viewer process, preventing repeated request-time joins and identity takeover churn. |
| `TK-73d77c3e7b18` | Fleet search is exposed as an ARIA combobox with keyboard semantics. |
| `TK-386af2514173` | Personal separates in-review work from other work states. |
| `TK-e371f76754a5` | The README links the verified showcase rather than relying on unverified illustrations. |
| `TK-143f427367aa` | Home runtime wheelhouse resolution is locked and reproducibly verified for the next train. |
| `TK-912f3657c2bf` | Wheelhouse resolver and verifier environments ignore checkout `PYTHONPATH`, so release verification exercises installed wheels rather than source-tree metadata. |
| `TK-c801f9c8a3d9` | Wait Bridge reconciles missed actionable offers from a bounded backlog snapshot. |
| `TK-0b66192a59f6` | AionUi exposes partial connection recovery rather than collapsing it into a terminal failure. |
| `TK-62a2d109171b` | Central recovers deadline work from stale SQLite transaction contexts. |
| `TK-d0fe0580eb1b` | Adds and repairs the Beta.1 release-day runbook, including archived wheelhouse and install rehearsal steps. |
| `TK-1d57f70a8d5e` | AionUi join retries preserve safe intermediate and recovery states. |
| `TK-e2759f0b4dad` | Repairs public beta links, routes, and wording across the website and public docs. |
| `TK-6ef5dcc0190f` | Fleet disconnected diagnostics distinguish configuration and runtime failures more clearly. |
| `TK-ad842243b0d8` | Central indexes journal sequences and keeps indexed reads consistent with transaction state. |
| `TK-beead2d80df1` | Test-only: makes the Central archive benchmark deterministic so the release gate is repeatable. |
| `TK-54645e4c45bf` | Test-only: forces stable no-color argparse help output so release checks do not vary with terminal color support. |
| `TK-81922613b811` | Corrects Beta.1 rc6 artifact provenance, wheelhouse instructions, and release-body wording. |
| `TK-6628af007951` (rebase of `TK-8cb76082947d`) | Removes focus from non-actionable Fleet rows, labels keyboard help, and announces live connection changes. |
| `TK-e94424871e67` | Publishes the exact Browser201 Beta.1 coverage boundary instead of implying a full 201-row pass. |
| `TK-939d37230fa3` | Preserves the immediate-offer reason while retaining the bridge's transport-mode label. |
| `TK-dc0f4a677709` | Test-only: isolates wait-bridge timing cases and removes false intermittent failures from the gate. |
| `TK-926aaf7d46d2` | ACP P0 adds the ACP seat client and its conformance coverage to the CI manifest. |
| `TK-9910ee9be69f` | Advances the reviewed exact MCP dependency set to 2.2.0. |
| `TK-6f9b26db13c0` | Documents the Central ACP client contract and operator direction. |
| `TK-9ddd07bb1d25` | Records the Beta.1 first-run blockers rather than presenting the path as fully verified. |
| `TK-707d0703a52e` | Makes the release runbook use an annotated `-a` tag and fail-closed chained commands, with optional signing. |
| `TK-be18d1cab59f` | Fixes first-run README/manual guidance for wheelhouse versus bundle, Python support, and Central health checks. |
| `TK-b05dc68eafc0` | Compact write envelopes bound model-facing Central responses while retaining receipts, errors, and review-lease expiry. |
| `TK-b84df54bfb56` | Updates public docs and the website to link the actually published Beta.1 release. |
| `TK-83fd9bffda99` | ACP P2 adds the ACP board agent, cancellation bound to the active prompt turn, and conformance coverage. |
| `TK-87f7d7051241` | Research-only: records Qoder Wake coordination UX findings without claiming a shipped UI change. |
| `TK-0267ecd5d14b` | Preserves the pre-beta CodeQL receipt and archives its release-history context. |
| `TK-735be746039c` | Reconciles the reviewed rc6 source lineage into the post-release `main` tree while preserving the salvaged security receipt. |
| `TK-d1ba904f0f53` | Re-bases the reviewed pairing workflow onto the reconciled tree. |
| `TK-fc3eb834372b` | Adds stable Fleet board selectors and board-selection state for Browser201 capture. |
| `TK-bc334fe4cdce` | Re-lands the reviewed ACP ticket set with the Linux case-normalised worktree fix. |
| `TK-68fff0e4699b` | Rejects stale foreign Personal runtime processes deterministically. |
| `TK-adeac4bbabe8` | Lands bounded offer catch-up behavior with a regenerated Personal component lock. |
| `TK-93e833c54d6c` | Re-scopes Personal Browser201 observations to the real MCP App surface. |
| `TK-6fa1eabbe4f3` | Adds Connect-helper origin and port prefill plus handoff tooling. |
| `TK-2366c38205b6` | Lands response-projection phase 2 with collision-safe identifier abbreviation. |
| `TK-4ff2f9f740ef` | Adds the helper startup bridge-compatibility handshake. |
| `TK-daee8b8ce82f` | Keys the Fleet unified seat pool by stable agent identity. |
| `TK-84e7e39328f5` | Records the reviewed Beta UI acceptance pass and 26 captured states. |
| `TK-9ca52e7ad8bf` | Records the Personal 79-row Browser201 feasibility boundary. |
| `TK-1430a4b5d7f2` | Stops ACP lease renewal before terminal mutations. |
| `TK-b58fdaba3a63` | Bounds board-digest transitions and reports omitted counts. |
| `TK-02bf4d01d662` | Adds the sourced Beta.2 known-limitations document and freeze markers. |
| `TK-e6660100174f` | Adds the event-driven board butler in shadow mode. |
| `TK-f0bfc072ebe0` | Makes the Fleet seat inventory route reachable to the acceptance selectors. |
| `TK-db6ca1290d33` | Reworks Fleet Agents into a laptop-sized six-column grid. |
| `TK-c3e4dbbd5dbb` | Rejects submissions whose declared tip is absent from the declared remote branch. |
| `TK-9893df303b34` | Re-lands the Personal component lock with its regenerated digest. |
| `TK-0d76c72150e1` | Regenerates the cumulative host integration manifest after the merged train. |
| `TK-e1cea022b42f` | Renders the bounded sanitized Fleet feed error, verified in a real browser. |
| `TK-97bde4aaaec2` | Captures and evaluates the Fleet Browser201 catalogue at the frozen base. |
| `TK-fe5f8a526ba8` | Retains `mcp==2.1.1` and corrects the install guidance. |

### Merged history that is not in the net Beta.2 scope

| Ticket | Disposition |
| --- | --- |
| `TK-59681c3e4bb1` | Its first README overhaul was reverted; `TK-3b17c3239aaa` is the retained replacement. |
| `TK-902db8904377` | Its first offer-reconciliation merge was reverted; the retained behavior comes from `TK-c801f9c8a3d9` and `TK-939d37230fa3`. |
| `TK-f905e3b8469d` | Its wait-mode implementation was reverted; the retained immediate-return contract comes from `TK-939d37230fa3`. |
| `TK-1bdfc3931bd3` | The first MCP 2.2.0 evaluation batch was reverted at `ce39302bb497f0c5a6995f56db8d29ba416c6216`; reviewed successor `TK-9910ee9be69f` later landed the retained exact 2.2.0 set. |
| `TK-1ec2ca97709b` | Its elicitation implementation was merged and later reverted; the dependency version is independently retained by `TK-9910ee9be69f`. |

## Version map

The released map comes from `v5.0.0b1:tools/release_versions.toml`. The net
tree comparison changes Central, Client, Personal, and Wait Bridge; Import is
unchanged. `release_train.py` couples `product`, `pursers`, and `personal`, so
those three advance together even though `packages/pursers` has no source
change.

| Key | Beta.1 | Planned Beta.2 | Reason |
| --- | --- | --- | --- |
| `product` | `5.0.0b1` | `5.0.0b2` | New prerelease train identity. |
| `pursers` | `5.0.0b1` | `5.0.0b2` | Coupled to `product`. |
| `personal` | `5.0.0b1` | `5.0.0b2` | Personal runtime/UI changed and is coupled to `product`. |
| `central` | `0.1.0a30` | `0.1.0a31` | Central runtime and response contracts changed. |
| `client` | `0.1.0a23` | `0.1.0a24` | Client event handling changed. |
| `wait_bridge` | `0.1.0a16` | `0.1.0a17` | Offer reconciliation and immediate-return behavior changed. |
| `import` | `5.0.0a3` | `5.0.0a3` | `packages/import` is unchanged; do not bump it. |

The candidate ticket must first run and review this dry-run:

```sh
python3 tools/release_train.py bump --dry-run \
  --set product=5.0.0b2 \
  --set central=0.1.0a31 \
  --set client=0.1.0a24 \
  --set wait_bridge=0.1.0a17
```

Only after the final `v5.0.0b1...B2_BASE_SHA` changed-path check confirms the
same package set may the candidate ticket run the identical command without
`--dry-run`. It must not use a blanket alpha bump because that would change
the unchanged Import package.

## Release gates

All gates in [`RUNBOOK-v5.0.0b1.md`](RUNBOOK-v5.0.0b1.md) remain applicable
with Beta.2's exact candidate SHA, newly built artifacts, newly approved
hashes, and new release body. Beta.1 hashes, browser captures, CI results, and
CodeQL results are historical evidence, not Beta.2 passes.

Before candidate review:

1. Freeze `B2_BASE_SHA`, apply only the reviewed version-map output, regenerate
   the cumulative host integration manifest with the repository-approved
   procedure, and commit the final manifest freeze. Record that final checksum
   commit as `B2_CANDIDATE_SHA`. Any later changed byte creates a new candidate
   and restarts candidate-bound gates.
2. Run `release_train.py check`; all manifest suites through
   `ci_manifest.py check`, `collect`, `run`, and `verify`; `leak_scan.py`;
   dashboard typecheck/build; Node extension tests; dependency audit; and
   `git diff --check`.
3. Build the six-wheel cohort, Home runtime wheelhouse, and AionUi archive
   twice in clean Python 3.12 environments. Require byte-for-byte equality,
   isolated installed imports, lifecycle probes, and exact candidate metadata.
4. Require the **host integration manifest to be green on the exact
   `B2_CANDIDATE_SHA`**: first verify that its path set covers the intended
   cumulative host integration change set, then replay
   `tools/aionui-extension/INTEGRATION_FILES.sha256`, run the host/helper and
   package suites from that same clean checkout, and bind the installed ZIP's
   `webui/candidate.json` plus receipt to that SHA. A green parent commit does
   not satisfy this gate.
5. Require exact-tip CI and all three CodeQL categories (Actions,
   JavaScript/TypeScript, and Python) to succeed for `B2_CANDIDATE_SHA`, with
   zero unresolved alerts selected by both exact ref and exact SHA. Recheck
   CodeQL after an unchanged candidate reaches `main`.
6. Obtain strict independent-principal review of source, artifacts, host
   manifest, browser evidence, and the literal executable gate outputs.
7. Require `origin/main`, the approved candidate, and the eventual annotated
   tag target to be the same commit before the operator follows the publish
   portion of the runbook.

### Browser201 evidence at the frozen base

All three implementation tickets below are closed, approved, and present in
`B2_BASE_SHA`. Their implementation status must not be confused with a fresh
Beta.2 candidate-bound browser pass:

| Ticket | Current acceptance boundary | Beta.2 disposition at this base |
| --- | --- | --- |
| `TK-5ddf2539f157` | The typed-evidence pipeline now emits fail-closed records and preserves trust-binding errors. | Implementation closed and approved. Its Full Access review replay was on the ticket candidate, not `B2_CANDIDATE_SHA`; AionUi still needs a fresh candidate-bound replay. |
| `TK-93e833c54d6c` | The 79 Personal rows now target the real Personal MCP App frame with `surface=mcp-app`, rather than a nonexistent top-level route. | Implementation closed and approved. No 79-row Personal capture is yet bound to `B2_CANDIDATE_SHA`. |
| `TK-fc3eb834372b` | Fleet now exposes the stable board selectors and selection state required by the harness. | Closed and approved. Operator evidence `AN-000000001087` binds the frozen base and records 93 SUCCESS / 0 FAILURE / 0 BLOCKED; do not relabel that base capture as a candidate-tip capture if the candidate bytes differ. |

Candidate acceptance still requires product-produced responses, captures,
typed evidence, and strict evaluation for any domain claimed complete on the
exact candidate. Worker fixtures or expected-value fabrication do not close a
Browser201 row.

## Candidate-assembly ticket text to file

```text
Title: Assemble and gate the immutable Pursers v5.0.0b2 candidate

Branch: codex/TK-86f2fcaebee3-b2-candidate-worker13
Base: exact frozen origin/main commit
0c83cd8da4e8ac04ee2dd564559f57718335cdef
(record this full SHA as B2_BASE_SHA before mutation)

Scope:
1. Fetch origin; prove B2_BASE_SHA is still the authorized main tip, contains
   86c89b59b851f5e50897aafeb84d4dada8d17735, and has a clean checkout. If main
   moved, stop rather than silently rebasing the frozen candidate.
2. Re-run git log and git diff from v5.0.0b1; reconcile every landed ticket,
   package-path change, Browser201 ticket, and coordinator decision with
   docs/releases/PLAN-v5.0.0b2.md. Stop for a revised decision if the planned
   map no longer matches.
3. Run release_train.py bump --dry-run with product=5.0.0b2,
   central=0.1.0a31, client=0.1.0a24, and wait_bridge=0.1.0a17. Review every
   generated path, then run the identical non-dry command. Leave Import at
   5.0.0a3. Add the reviewed Beta.2 changelog and release body required by the
   release helper; do not hand-edit generated version consumers.
4. Commit the generated version/release changes. Regenerate the cumulative
   tools/aionui-extension/INTEGRATION_FILES.sha256 path set and hashes with the
   reviewed repository procedure, independently replay it, and commit the
   manifest freeze. Record that final checksum commit as the literal full
   B2_CANDIDATE_SHA. Build no candidate artifact before this commit and merge
   no later change into the candidate.
5. From fresh checkouts and clean Python 3.12 environments, run the complete
   manifest, leak, diff, dashboard, Node, dependency, deterministic artifact,
   isolated-install, lifecycle, host integration-manifest, and Browser201
   gates in the plan. Bind every observation and artifact receipt to
   B2_CANDIDATE_SHA.
6. Require exact-ref and exact-SHA CI plus all three CodeQL analyses at the
   candidate tip, zero open exact-candidate alerts, and independent-principal
   review of the same bytes.
7. Submit branch_and_commit, tip-only files_changed, approved_base,
   cumulative_files_changed, literal test-command:/test-output: lines, artifact
   hashes, host-manifest evidence, Browser201 dispositions, CodeQL evidence,
   and observations. Do not tag, publish, deploy, or push main in this ticket.

Forbidden: unrelated source changes, version changes outside release_train.py,
moving/reusing a tag, publishing, deployment, or treating a historical gate as
an exact-candidate pass.
```

## Risks and rollback

- `origin/main` moving away from `B2_BASE_SHA` voids this candidate; never
  silently rebase a frozen release candidate.
- The base host integration manifest is green, but version changes alter
  covered files. Candidate assembly remains blocked until the final cumulative
  manifest is regenerated, independently replayed, and frozen before artifact
  construction.
- MCP 2.2.0 is the exact reviewed dependency in the frozen tree. It remains
  incompatible with FastMCP 2.13.1 in one environment, so the release keeps
  the dedicated-virtual-environment guidance from `TK-fe5f8a526ba8`.
  Response-projection phase 2 and the reviewed ACP ticket set are present.
- Browser201 implementation tickets are closed, but Personal and AionUi still
  lack fresh evidence bound to `B2_CANDIDATE_SHA`. Beta.2 must publish those
  evidence boundaries rather than imply a full 201-row candidate pass.
- Exact-tip CodeQL is a hard gate. A local test or historical green scan cannot
  waive a current alert.
- Component versions and published artifacts are immutable. A mismatch after
  the version commit requires a new candidate and, after publication, a new
  prerelease version; never overwrite an asset or move a tag.

The rollback target is the published `v5.0.0b1` tag at
`dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5`. Follow
[`RUNBOOK-v5.0.0b1.md` section 10](RUNBOOK-v5.0.0b1.md#10-stop-and-rollback-rules):
before tag push, discard staging and rebuild; after tag push, do not move the
tag; after publication, preserve evidence, mark availability only through an
explicit incident decision, recover to Beta.1, and advance to a new version.
