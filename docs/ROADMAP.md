# Pursers roadmap

Pursers 5.0.0 is the current supported release. This page separates released
work, near-term candidates, and research so that an experiment is never
mistaken for a commitment. Items move only after implementation, independent
review, and the applicable release gates pass.

Release artifacts and their checksum manifests are available from the generic
[GitHub Releases page](https://github.com/swisspra/Pursers/releases).

## Shipped in `v5.0.0`

The release cohort contains `pursers==5.0.0` and
`pursers-personal==5.0.0`, with Central `0.1.0`, Client `0.1.0`, Personal
Import `5.0.0`, Wait Bridge `0.1.0`, and ACP `0.1.0`.

Release highlights:

- The release path builds the Python distributions, AionUi extension ZIP, and
  locked Home runtime wheelhouse from the tagged source. The Home integration
  retains its exact-candidate source, browser, behavior, CI, and CodeQL
  verification boundary.
- Project-registry entries can bind work to an exact, board-scoped HTTPS
  repository URL. Generated seats fail closed when repository, checkout,
  board, or operator-ownership routing is unsafe; registry administration uses
  compare-and-set updates.
- Fleet Dashboard CodeQL findings were remediated: sensitive-key redaction no
  longer uses a polynomial-backtracking pattern, staged-wheel logs do not emit
  the environment-derived path, PyPI test routing checks hostnames, and test
  script-tag extraction handles upper-case tags.

Central binds to loopback by default. Remote access requires an
operator-supplied TLS certificate and key plus an allowed host. macOS is the
tested platform, and the MCP Apps dashboard remains read-only. See the
corresponding entry in the [changelog](../CHANGELOG.md) for the release record.

## Near-term candidates

The following work is ticketed and being evaluated for a future release. These
are candidate titles, not a promise that every item will ship:

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
