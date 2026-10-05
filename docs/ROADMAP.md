# Pursers roadmap

Pursers 5.2.0 is the current release train. This page separates released
work, near-term candidates, and research so that an experiment is never
mistaken for a commitment. Items move only after implementation, independent
review, and the applicable release gates pass.

Release artifacts and their checksum manifests are available from the generic
[GitHub Releases page](https://github.com/swisspra/Pursers/releases).

## `v5.2.0` release train

The release cohort contains `pursers==5.2.0` and
`pursers-personal==5.2.0`, with Central `0.1.8`, Client `0.1.9`, Personal
Import `5.0.0`, Wait Bridge `0.1.7`, and ACP `0.1.8`.

Planned release highlights, subject to the final prerequisite below:

- Nocturne adds source-backed Inbox, Work, Settings, project, team, and
  approval routes while preserving explicit authorization and evidence bounds.
- Fleet Settings adds guarded preview/apply/readback contracts for connector,
  onboarding, delivery, seat, membership, retention, Butler, and diagnostic
  families.
- Managed ACP setup can select exact Registry pins and session options, but
  provider-authenticated execution remains fail-closed until a dedicated
  auth/egress adapter is configured.
- Central avoids duplicate state encoding while preserving generation fences;
  wait and managed-seat recovery preserve positive cursors, leases, and paid
  model turns across typed transient transport failures.
- The supplied Pursers brand is included in public repository and Fleet
  surfaces, and dashboard dependency updates are covered by type and bundle CI.

At release-train preparation time, PR 83's Settings layout/browser regression
gate remained a prerequisite rather than verified deployed behavior. The final
tag requires its independently approved source plus full strict CI and browser
acceptance on the integrated candidate.

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
