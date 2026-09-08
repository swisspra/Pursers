# Superdesign Canvas Handoff

## Canvas

- Project: `c48769c8-8664-4e23-8490-f7af271a5849`
- Canvas: https://superdesign.dev/teams/3228802f-87e9-4bde-9d66-3544a6c38c24/projects/c48769c8-8664-4e23-8490-f7af271a5849
- Superdesign CLI: `0.14.0`
- Draft model: `gpt-5.6-luna`
- Model catalog description: `Fast & affordable GPT-5.6`
- Catalog limitation: no numeric draft context ceiling or per-call credit quote was exposed by `list-models gpt-5.6-luna --json`.
- Actual draft credits consumed: `144.0` total: `61.5` for the baseline, two Home directions, and Home QA refinements; `82.5` for the six Direction A flow pages. Two deterministic accessibility imports consumed no credits.
- No image or video generation, asset purchase, plan upgrade, or external brand asset was used.

## Drafts

| Purpose | Draft | Version | Preview |
| --- | --- | ---: | --- |
| Faithful current Personal dashboard baseline | `ee6dcdae-d401-4b66-851a-68ffc2d3a05b` | 1 | https://p.superdesign.dev/draft/ee6dcdae-d401-4b66-851a-68ffc2d3a05b |
| Direction A: Warm Guided Home | `3c7bf43a-9ae1-451d-8fbc-97a9468c2fab` | 4 | https://p.superdesign.dev/draft/3c7bf43a-9ae1-451d-8fbc-97a9468c2fab |
| Direction B: Calm Project Home | `2bcc013f-c1b0-4fc6-a6f7-a7b8fc62ceb2` | 4 | https://p.superdesign.dev/draft/2bcc013f-c1b0-4fc6-a6f7-a7b8fc62ceb2 |
| Projects: Warm Guided Home | `69b3340a-b9fd-47ab-9882-79fda80ac431` | 2 | https://p.superdesign.dev/draft/69b3340a-b9fd-47ab-9882-79fda80ac431 |
| Work: Warm Guided Home | `338327db-9531-43a3-ab58-63f20617d7f3` | 1 | https://p.superdesign.dev/draft/338327db-9531-43a3-ab58-63f20617d7f3 |
| Team: Warm Guided Home | `274d5b8a-c348-4193-8ffc-6166a899927a` | 1 | https://p.superdesign.dev/draft/274d5b8a-c348-4193-8ffc-6166a899927a |
| Approvals: Warm Guided Home | `f928178a-b5e2-4de9-a1ba-c6880499efa4` | 1 | https://p.superdesign.dev/draft/f928178a-b5e2-4de9-a1ba-c6880499efa4 |
| Activity: Warm Guided Home | `7c2fd485-4cb0-409c-8fb4-f76275edb548` | 2 | https://p.superdesign.dev/draft/7c2fd485-4cb0-409c-8fb4-f76275edb548 |
| Settings: Warm Guided Home | `44799faf-b589-43b1-85db-b1ab705e9fb1` | 2 | https://p.superdesign.dev/draft/44799faf-b589-43b1-85db-b1ab705e9fb1 |

## Selection

### Direction A — Warm Guided Home

Warm sand and off-white surfaces, restrained teal status/action color, a human welcome, and a clear review-first next step. It feels closest to a calm household dashboard and makes WORK/PERSONAL context switching prominent.

### Direction B — Calm Project Home

Cool gray and white surfaces, navy text, cobalt action color, and a compact AionUI product bar. It leads with project cards that answer connection, Team, running-work, and pending-action questions before showing the selected project. It suits returning multi-project users while remaining much lighter than the current control-plane dashboards.

Direction A was selected by the operator through `HR-d68423777f76a864`. It is the `/home` active draft and the source of the six sibling pages. Direction B remains preserved as an unselected alternative.

## Design Context

The complete external context bundle contains 11 repo-relative files:

1. `.superdesign/design-system.md`
2. `.superdesign/context/primary-dashboard-current.md`
3. `.superdesign/init/components.md`
4. `.superdesign/init/layouts.md`
5. `.superdesign/init/routes.md`
6. `.superdesign/init/theme.md`
7. `.superdesign/init/pages.md`
8. `.superdesign/init/extractable-components.md`
9. `docs/design-home/journeys.md`
10. `tools/dashboard-ui/dashboard-entry.html`
11. `tools/dashboard-ui/src/dashboard.css`

The 404280-byte built dashboard and full 1528-line TypeScript module were deliberately excluded from upload. Their source fingerprints and bounded render contract are recorded in `.superdesign/context/primary-dashboard-current.md`.

Current source baseline: `origin/main@c2ebac5de803a0f7a00468ec4d3cdf06e4719096`. Inventory comparison candidate `codex/TK-f8a62bab8d05-resubmit-6@10f629af25ff841d9ceafaf377faf7daa598c906` was rejected after this repair and is not called authoritative. The six init files and their documentation mirrors were checked directly against the baseline. The route matrix now matches the complete mechanically extracted GET and POST handler sets, including live `#/seats`, GET `/api/attention`, GET and POST `/api/intake`, and GET `/api/board/<board>`. Existing drafts were historically generated from the earlier resubmit-1 bundle; this source-context refresh postdates generation, and no claim is made otherwise.

Approved journey dependency: `codex/TK-1f8315536a3e@8202422c6841b84a6aeab64f6722e93805a3c86a`.

No repository logo/image asset exists for these surfaces. The canvas therefore preserves the source's CSS/text `OB` or `P` identity instead of inventing or uploading a logo.

## Reusable Components

| Component | ID | Source |
| --- | --- | --- |
| PersonalAppHeader | `109147bc-4b43-4031-9369-4da243983f86` | `.superdesign/components/personal-app-header.html` |
| PersonalViewTabs | `815d3747-4206-44a4-a17b-f5198b9198a6` | `.superdesign/components/personal-view-tabs.html` |
| FleetSidebarNav | `907f4715-ac40-4c2b-8de8-e6afb34812f5` | `.superdesign/components/fleet-sidebar-nav.html` |

## Validation

- Live-preview semantic inspection confirmed the faithful baseline preserves the current labels, six tabs, health, metrics, current work, agents, handoff, pinned decision, recent activity, and read-only/demo/bounded truth labels.
- Live-preview visual inspection confirmed the baseline reproduces the current dark navy hierarchy and card composition.
- Direction A and Direction B were visually inspected on desktop and responsive mobile layouts in a real browser.
- Final 390px checks for both branches: `documentElement.scrollWidth === clientWidth === 390`; no horizontal page overflow.
- Final 390px checks for both branches: zero visible `button`, `a`, or `input` targets below 44px in either dimension where applicable.
- Desktop visual inspection covered all six sibling pages at 1440px in a real Chromium browser.
- Final 390px checks for Projects, Work, Team, Approvals, Activity, and Settings: `scrollWidth === clientWidth === 390`; no horizontal page overflow.
- Final 390px checks for all six sibling pages: zero visible `button`, `a`, `input`, `select`, or `summary` targets below 44px in either dimension.
- Projects, Activity, and Settings initially contained undersized disclosure summaries. Deterministic no-credit imports raised those hit targets to 44px and added visible focus styling; browser QA then passed.
- Both candidates expose Home, Projects, Work, Team, Approvals, Activity, and Settings.
- Direction A implements the approved next-action Home; Direction B implements the approved project Home while keeping WORK and PERSONAL isolated.
- Direction A was refined after QA removed a contradictory first-run prompt from a populated returning-user state.
- Direction B was refined after QA removed mobile clipping and changed the unsupported `Open in agent chat` control to `View work details`.
- Context validation confirms all six `.superdesign/init/*.md` files match their `docs/design-home/context/*.md` mirrors. The route matrix matches the exact baseline handler sets, `pages.md` retains the live `#/seats` route, and every resume target fingerprints the current 11-file bundle.

## Limitations and Next Step

- These are design drafts, not implemented application UI.
- The canvas covers the faithful baseline, both Home alternatives, and the complete Direction A family: Home, Projects, Work, Team, Approvals, Activity, and Settings.
- Navigation and buttons demonstrate information architecture; they do not prove host APIs or backend mutations exist.
- HTML inspection was used only for structural validation. Visual claims come from live Superdesign previews rendered in a real browser.
- The context refresh postdates draft generation. Existing drafts and exports were reconciled against it without spending credits or implying regeneration.
- Reusable tokens, page contracts, and HTML references are published in `docs/design-home/design-tokens.css`, `docs/design-home/page-specs.md`, and `docs/design-home/exports/`.
