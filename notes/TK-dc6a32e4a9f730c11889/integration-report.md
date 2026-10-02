# Experience 07 integration report

## Integrated reviewed inputs

The core integration branch starts from approved base
`1b0c2216fa4cf90b3f5347a372653c079d80d356` and preserves the complete reviewed
histories for these inputs:

| Stream | Reviewed branch and commit |
| --- | --- |
| Durable lifecycle activity | `codex/TK-c06ebd9d45c85ba02fc4-worker13-fix1@3afcfe03f56cd83cd2fe938de649324a52c8ddd1` |
| Ticket and retained-history pagination | `codex/TK-d92cd2d18287ba7508f6-pagination@40db2fc3d37c7b3f6341128a5c37d7524b239c0c` |
| Board-scoped display names | `codex/TK-257d7ad39e0560d609ae-display-names@ce9c890144486cb14df151b47a404dc65e57fc3e` |
| Lazy resources and prompts | `codex/TK-a87856b536d8956ed5a0-experience-03@16d0c010e9962edb9a7ced0be827d37c9dc4d68e` |
| Portable role skills | `codex/TK-baaaa88bbbc0bda03eff-portable-skills@bffd048cc2f273b75ed524780d830974ed30f839` |

The website remains in its repository. Its integration branch starts from
`71eb3a8426f93d08b5c3689d15f3c45c7524693b` and preserves the complete reviewed
website history through
`codex/TK-263a6469c16949bdf499-csp-verifier3@771719146ceaeab71b8cdf7cf037327903c984ce`.
Browser evidence is committed at
`codex/TK-dc6a32e4a9f730c11889-web-integration-worker12@09253bea1f75d4a51f8b1091312d71372166bdba`.

Shared-file merge conflicts were limited to additive changelog and documentation
sections, Central's package bridge module list, and the Work-view tests. The
resolution retains both pagination and durable-activity tests and registers both
`ticket_pagination` and `agent_profile` without changing either stream's public
contract.

## Cumulative core files from approved base

Exact output of `git diff --name-only 1b0c2216fa4cf90b3f5347a372653c079d80d356..HEAD`:

```text
CHANGELOG.md
artifacts/TK-257d7ad39e0560d609ae/browser-acceptance.md
artifacts/TK-257d7ad39e0560d609ae/revision-conflict.png
artifacts/TK-257d7ad39e0560d609ae/xss-duplicate-display-names.png
delivery-manifest.toml
docs/ARCHITECTURE.md
docs/GETTING-STARTED.md
docs/design-home/ticket-progress-contract.md
docs/guides/adding-agents.md
docs/guides/connecting-clients.md
docs/guides/fleet-dashboard.md
docs/guides/portable-skills.md
docs/guides/running-a-fleet.md
docs/guides/zed.md
docs/reference/mcp-discovery.md
integrations/skills/manage.py
integrations/skills/manifest.json
integrations/skills/pursers-operate/SKILL.md
integrations/skills/pursers-operate/references/butler.md
integrations/skills/pursers-operate/references/coordinator.md
integrations/skills/pursers-operate/references/integration.md
integrations/skills/pursers-operate/references/release.md
integrations/skills/pursers-review/SKILL.md
integrations/skills/pursers-review/references/reviewer-scenarios.md
integrations/skills/pursers-start/SKILL.md
integrations/skills/pursers-start/references/hosts.md
integrations/skills/pursers-work/SKILL.md
integrations/skills/pursers-work/references/evidence-and-recovery.md
notes/TK-c06ebd9d45c85ba02fc4/browser-evidence.md
notes/TK-c06ebd9d45c85ba02fc4/browser/work-activity-mobile.png
notes/TK-c06ebd9d45c85ba02fc4/browser/work-activity-reduced-motion.png
notes/TK-c06ebd9d45c85ba02fc4/handoff.md
notes/TK-dc6a32e4a9f730c11889/integration-report.md
packages/central/README.md
packages/central/src/pursers_central/__init__.py
packages/central/src/pursers_central/activity.py
packages/central/src/pursers_central/agent_profile.py
packages/central/src/pursers_central/central.py
packages/central/src/pursers_central/ticket_pagination.py
packages/central/tests/benchmark_ticket_pagination.py
packages/central/tests/test_agent_display_name.py
packages/central/tests/test_response_bounds.py
packages/central/tests/test_ticket_activity.py
packages/central/tests/test_ticket_pagination.py
packages/central/tests/test_ticket_progress.py
packages/client/src/pursers_client/__init__.py
packages/client/src/pursers_client/client.py
packages/client/src/pursers_client/discovery.py
packages/client/src/pursers_client/events.py
packages/client/src/pursers_client/mcp_proxy.py
packages/client/tests/test_mcp_proxy.py
packages/client/tests/test_per_call_identity.py
tools/board-butler/board_butler.py
tools/board-butler/tests/test_integration_delivery.py
tools/check_delivery_manifest.py
tools/fleet-dashboard/activity_visibility.py
tools/fleet-dashboard/fleet_dashboard.py
tools/fleet-dashboard/tests/display_acceptance_server.py
tools/fleet-dashboard/tests/test_activity_visibility.py
tools/fleet-dashboard/tests/test_fleet_dashboard.py
tools/fleet-dashboard/tests/test_work_view.py
tools/fleet-dashboard/ui/assets/app.js
tools/fleet-dashboard/ui/views/team.css
tools/fleet-dashboard/ui/views/team.js
tools/fleet-dashboard/ui/views/work.css
tools/fleet-dashboard/ui/views/work.js
tools/tests/test_skill_bundle.py
tools/wait-bridge/pursers_wait_server.py
tools/wait-bridge/tests/test_orchestrator_mode.py
```


## Seat suite report

The directly affected core suites ran together against the integrated checkout:

```text
TMPDIR=/PATH/TO/USER-CACHE PYTHONPATH=packages/central/src:packages/client/src \
python3 -m pytest -q \
  packages/central/tests/test_ticket_pagination.py \
  packages/central/tests/test_ticket_activity.py \
  packages/central/tests/test_ticket_progress.py \
  packages/central/tests/test_response_bounds.py \
  packages/central/tests/test_agent_display_name.py \
  packages/client/tests/test_mcp_proxy.py \
  packages/client/tests/test_per_call_identity.py \
  tools/fleet-dashboard/tests/test_work_view.py \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py \
  tools/fleet-dashboard/tests/test_activity_visibility.py \
  tools/wait-bridge/tests/test_orchestrator_mode.py \
  tools/board-butler/tests/test_integration_delivery.py \
  tools/tests/test_skill_bundle.py
```

Result: `509 passed, 11 subtests passed in 200.64s (0:03:20)`.

This covers the data-producing Central and client paths for authorization,
pagination, retained history, durable work/review/delivery activity, retry
identity, no progress-driven lease renewal, reconnect projection, display-name
identity preservation, lazy discovery, legacy-host fallback, dashboard rendering,
delivery boundaries, and portable-skill installation/recovery.

Additional checks:

```text
python3 tools/check_delivery_manifest.py
delivery manifest OK

python3 tools/leak_scan.py
leak_scan: clean (0 violations)

git diff --check
passed with no output
```

The website ran `npm run check`: Astro reported zero errors, warnings, or hints;
seven routes were built; Vitest passed 14 tests in two files; and Wrangler's
deployment dry-run completed. Its tracked-file leak scan and `git diff --check`
also passed. No deployment or live configuration change was made.

The worker validation rule dated 2026-09-28 prohibits use of the `ci_manifest`
full-gate queue for tickets outside release-train scope. This ticket has no
release-train scope, so no `ci_manifest` queue command was run. Directly affected
suites and required leak/diff checks were used instead.

## Product-produced measurement evidence

`packages/central/tests/benchmark_ticket_pagination.py` exercised the integrated
Central server and product `ticket_list` responses with archived tickets:

| Tickets | Legacy-emulated bytes / hydrated | Bounded one-shot bytes / returned / hydrated | Paged first / max / total bytes | Pages / IDs / hydrated |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 104,211 / 100 | 104,410 / 100 / 100 | 52,741 / 52,741 / 105,172 | 2 / 100 / 100 |
| 1,000 | 520,548 / 500 | 191,654 / 184 / 184 | 52,730 / 52,868 / 1,055,300 | 20 / 1,000 / 1,000 |

The 1,000-ticket bounded response returns 184 rows within its byte limit instead
of hydrating 500. A complete traversal returns all 1,000 unique IDs across 20
pages. The total traversal is intentionally larger than one bounded page; callers
pay for complete history only when they request it.

Website browser and page-weight evidence is stored in the website repository at
`artifacts/TK-dc6a32e4a9f730c11889/browser-evidence.md`. Chromium exercised the
served routes, real 404, API health response, Docs filtering and copy interaction,
desktop layout, 390 by 844 mobile layout, lazy images, and security headers.

## Configuration, migration, and rollback

- New Central records and response fields are additive. Older clients may ignore
  pagination, activity, display-name, and discovery fields.
- Pagination cursors are bound to board, principal, filters, ordering, view, and
  archive mode. Clients restart without a cursor after an authorization/filter or
  key-rotation mismatch.
- Durable activity remains a projection of authoritative workflow facts. Existing
  `ticket_progress_v1` configuration still controls optional estimates; updates
  do not renew leases.
- Display names are board-scoped aliases on the exact stable identity. Rollback
  restores operational-name rendering while retaining additive profile records.
- Lazy resources and prompts fall back to short tool help and canonical docs when
  a host does not support resource/prompt discovery.
- Portable skill installation is preview-first, preserves conflicts, and removes
  only exact managed copies. Rollback uses its documented removal path.
- Website rollback restores the prior Vite entry and SPA asset mode together.
  Do not keep the SPA fallback with the Astro route tree.
- No feature flag, service, credential, deployment, main branch, release version,
  or delivery policy was changed by this integration.

## Operator-owned generated refreshes

The integration intentionally does not modify generated artifacts. Operator
refresh is required before a release candidate:

- `tools/aionui-extension/INTEGRATION_FILES.sha256` reports six mismatches:
  `packages/central/src/pursers_central/central.py`,
  `packages/client/src/pursers_client/client.py`,
  `packages/client/tests/test_per_call_identity.py`,
  `tools/fleet-dashboard/fleet_dashboard.py`,
  `tools/fleet-dashboard/tests/test_fleet_dashboard.py`, and
  `tools/wait-bridge/pursers_wait_server.py`.
- `component-lock.json` needs new Central members `activity.py`,
  `agent_profile.py`, and `ticket_pagination.py`; new Client member
  `discovery.py`; and refreshed digests for the changed Central/Client package
  members reported by `tools/release_train.py check`.
- The reference generator requires grouping/scope metadata for
  `agent_display_name_set` and `ticket_history_list` before generated reference
  pages can be refreshed.

These are release-candidate refresh tasks, not product test failures. The source
integration, direct suites, browser acceptance, and dry-run deployment checks
remain independently reproducible before that operator step.
