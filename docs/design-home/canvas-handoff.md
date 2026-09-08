# Superdesign Canvas Handoff

## Canvas

- Project: `c48769c8-8664-4e23-8490-f7af271a5849`
- Canvas: https://superdesign.dev/teams/3228802f-87e9-4bde-9d66-3544a6c38c24/projects/c48769c8-8664-4e23-8490-f7af271a5849
- Superdesign CLI: `0.14.0`
- Draft model: `gpt-5.6-luna`
- Model catalog description: `Fast & affordable GPT-5.6`
- Catalog limitation: no numeric draft context ceiling or per-call credit quote was exposed by `list-models gpt-5.6-luna --json`.
- Actual draft credits consumed: `46.5` across baseline, two initial branches, and four targeted QA refinements.
- No image or video generation, asset purchase, plan upgrade, or external brand asset was used.

## Drafts

| Purpose | Draft | Version | Preview |
| --- | --- | ---: | --- |
| Faithful current Personal dashboard baseline | `ee6dcdae-d401-4b66-851a-68ffc2d3a05b` | 1 | https://p.superdesign.dev/draft/ee6dcdae-d401-4b66-851a-68ffc2d3a05b |
| Direction A: Warm Guided Home | `3c7bf43a-9ae1-451d-8fbc-97a9468c2fab` | 3 | https://p.superdesign.dev/draft/3c7bf43a-9ae1-451d-8fbc-97a9468c2fab |
| Direction B: Calm Focused Workspace | `2bcc013f-c1b0-4fc6-a6f7-a7b8fc62ceb2` | 3 | https://p.superdesign.dev/draft/2bcc013f-c1b0-4fc6-a6f7-a7b8fc62ceb2 |

## Selection

### Direction A — Warm Guided Home

Warm sand and off-white surfaces, restrained teal status/action color, a human welcome, and a clear review-first next step. It feels closest to a calm household dashboard and makes WORK/PERSONAL context switching prominent.

### Direction B — Calm Focused Workspace

Cool gray and white surfaces, navy text, cobalt action color, a compact AionUI product bar, and a structured work timeline. It feels closer to a professional workspace while remaining much lighter than the current control-plane dashboards.

The baseline remains `activeDraftId` until the user chooses. Do not silently select a branch.

## Design Context

The complete external context bundle contains 10 repo-relative files:

1. `.superdesign/design-system.md`
2. `.superdesign/context/primary-dashboard-current.md`
3. `.superdesign/init/components.md`
4. `.superdesign/init/layouts.md`
5. `.superdesign/init/routes.md`
6. `.superdesign/init/theme.md`
7. `.superdesign/init/pages.md`
8. `.superdesign/init/extractable-components.md`
9. `tools/dashboard-ui/dashboard-entry.html`
10. `tools/dashboard-ui/src/dashboard.css`

The 404280-byte built dashboard and full 1528-line TypeScript module were deliberately excluded from upload. Their source fingerprints and bounded render contract are recorded in `.superdesign/context/primary-dashboard-current.md`.

Context source dependency: `codex/TK-f8a62bab8d05-resubmit-1@dfa4fa535bbc9aa665f0d8856971472651659af5`.

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
- Both candidates expose Home, Projects, Work, Team, Approvals, Activity, and Settings.
- Direction A was refined after QA removed a contradictory first-run prompt from a populated returning-user state.
- Direction B was refined after QA removed mobile clipping and changed the unsupported `Open in agent chat` control to `View work details`.

## Limitations and Next Step

- These are design drafts, not implemented application UI.
- The canvas currently covers the faithful existing Home-equivalent and two redesigned Home candidates. It does not yet contain sibling page drafts.
- Full Projects, Work, Team, Approvals, Activity, and Settings drafts wait at the required user-selection boundary.
- Navigation and buttons demonstrate information architecture; they do not prove host APIs or backend mutations exist.
- HTML inspection was used only for structural validation. Visual claims come from live Superdesign previews rendered in a real browser.
- After the user selects A or B, use `execute-flow-pages` from that exact draft and persist each returned page as its own target in `.superdesign/resume.json`.

