# Pursers roadmap

Pursers is entering public beta. This page separates released work, near-term
candidates, and research so that an experiment is never mistaken for a
commitment. Items move only after implementation, independent review, and the
applicable release gates pass.

> **Publication note:** The `v5.0.0b1` tag and release publication are pending.
> The coordinator removes this note when the operator publishes the approved
> candidate.

## Shipped in `v5.0.0b1`

The first beta release contains `pursers==5.0.0b1` and
`pursers-personal==5.0.0b1`, with Central `0.1.0a30`, Client `0.1.0a23`,
Personal Import `5.0.0a3`, and Wait Bridge `0.1.0a16`.

Release highlights:

- The release path preserves GitHub prerelease status and prevents beta tags
  from becoming the stable latest release. The Home integration entered its
  exact-candidate source, browser, behavior, CI, and CodeQL verification
  boundary.
- Project-registry entries can bind work to an exact, board-scoped HTTPS
  repository URL. Generated seats fail closed when repository, checkout,
  board, or operator-ownership routing is unsafe; registry administration uses
  compare-and-set updates.
- Fleet Dashboard CodeQL findings were remediated: sensitive-key redaction no
  longer uses a polynomial-backtracking pattern, staged-wheel logs do not emit
  the environment-derived path, PyPI test routing checks hostnames, and test
  script-tag extraction handles upper-case tags.

This remains a single-owner, single-machine beta. It is not a stable release,
not intended for remote or untrusted multi-user deployment, and does not claim
a supported MCP Apps host yet. See the corresponding entry in the
[changelog](../CHANGELOG.md) for the release record.

## Beta.2 candidates

The following work is ticketed and being evaluated for the next beta. These
are candidate titles, not a promise that every item will ship in Beta.2:

- Runnable `pursers-central` entry point
- Wait-bridge offer reconciliation
- Harness candidate-diff-check fix
- Pairing automation
- UX audit follow-ups

Each candidate still needs its bounded implementation, independent review, and
release-train verification before it can be described as shipped.

## Under research — not committed

These topics are investigations only. They have no promised scope, release, or
delivery date:

- Linux CI hosts for capture workflows
- A trusted-verifier token model
- Multi-owner boards

Research may lead to a proposal, a different design, or no product change.

## How to influence the roadmap

Use [GitHub Issues](https://github.com/swisspra/Pursers/issues) to
share use cases and constraints:

- Open a **feature request** when you can describe a problem and the outcome
  you need.
- Open a focused issue when current behavior or documentation is unclear.
- Share a sanitized working setup or integration when it may inform future
  priorities.

The [community guide](community/DISCUSSIONS.md) explains response
expectations and how a public conversation may become a bounded board ticket.
A public issue is useful evidence, but it is not itself a roadmap commitment.
